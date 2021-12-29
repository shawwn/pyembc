import pdb
import sys
import os
import ctypes
import struct
from enum import Enum, auto
from typing import Type, Any, Iterable, Dict, Optional, Mapping
import tempfile
import atexit
import shutil
from get_annotations import get_annotations

tempfolder = tempfile.mkdtemp()
atexit.register(shutil.rmtree, tempfolder)

__all__ = [
    "emb_struct",
    "emb_union"
]

# save the system's endianness
_SYS_ENDIANNESS_IS_LITTLE = sys.byteorder == "little"
#  name for holding emb fields and endianness
_FIELDS = "__emb_fields__"
_ENDIAN = "__emb_endian__"
# name of the field in ctypes instances that hold the struct char
_CTYPES_TYPE_ATTR = "_type_"
# name of the field in ctypes Structure/Union instances that hold the fields
_CTYPES_FIELDS_ATTR = "_fields_"
# name of the field in ctypes Structure/Union instances that hold packing value
_CTYPES_PACK_ATTR = "_pack_"
# name of the field in ctypes Structure instances that are non-native-byteorder
_CTYPES_SWAPPED_ATTR = "_swappedbytes_"

def ero(*args):
    print(*args, file=sys.stderr)


def brk():
    mypdb = pdb.Pdb(stdout=sys.__stderr__)
    mypdb.reset()
    mypdb.set_trace()

def pm():
    mypdb = pdb.Pdb(stdout=sys.__stderr__)
    mypdb.reset()
    mypdb.interaction(None, sys.last_traceback)

def c_is_array_type(ctype):
    return issubclass(ctype, ctypes.Array)

def c_is_union_type(ctype):
    return issubclass(ctype, ctypes.Union)

def c_is_struct_type(ctype):
    return issubclass(ctype, ctypes.Structure)

def c_elem_type(ctype):
    if issubclass(ctype, ctypes.Structure):
        return ''.join([c_elem_type(t) for name, t in ctype._fields_])
    if issubclass(ctype, ctypes.Union):
        return max([c_elem_type(t) for name, t in ctype._fields_], key=len)
    if issubclass(ctype, ctypes.Array):
        return ctype._length_ * c_elem_type(ctype._type_)
    if issubclass(ctype, ctypes._SimpleCData):
        result = ctype._type_
        if not isinstance(result, str):
            brk()
            raise ValueError("Not a str")
        return result
    if issubclass(ctype, ctypes._Pointer):
        # raise ValueError("Can't infer the elem_type of a pointer")
        return "P"
    raise NotImplementedError("Unexpected ctype")

def c_elem_size(ctype):
    # byte_size = struct.calcsize(elem_type.replace('z', 'Z').replace('Z', 'P'))
    return ctypes.sizeof(ctype)

def c_short_type_name(ctype):
    if issubclass(ctype, ctypes.Array):
        return f'({c_short_type_name(ctype._type_)})[{ctype._length_}]'
    if issubclass(ctype, ctypes.Structure):
        return '{' + ','.join([c_short_type_name(t) for name, t in ctype._fields_]) + '}'
    if issubclass(ctype, ctypes.Union):
        return '{' + '|'.join([c_short_type_name(t) for name, t in ctype._fields_]) + '}'
    elem_type = c_elem_type(ctype)
    # noinspection PyUnresolvedReferences
    # byte_size = struct.calcsize(elem_type.replace('z', 'Z').replace('Z', 'P'))
    byte_size = c_elem_size(ctype)
    bit_size = byte_size * 8
    # noinspection PyUnresolvedReferences
    signedness = 'u' if elem_type.isupper() else 's'
    if issubclass(ctype, (ctypes.c_float, ctypes.c_double)):
        prefix = 'f'
    else:
        prefix = signedness
    return f"{prefix}{bit_size}"

def c_repr(cdata):
    if isinstance(cdata, (bytes, str, int, bool, float, type(None))):
        return repr(cdata)
    ctype = type(cdata)
    if isinstance(cdata, ctypes.Array):
        return '[' + ','.join([c_repr(x) for x in cdata]) + ']'
    if isinstance(cdata, ctypes.Structure):
        return '{' + ','.join([c_repr(getattr(cdata, name)) for name, t in ctype._fields_]) + '}'
    if isinstance(cdata, ctypes.Union):
        return '{' + '|'.join([c_repr(getattr(cdata, name)) for name, t in ctype._fields_]) + '}'
    if issubclass(ctype, (ctypes.c_float, ctypes.c_double)):
        return f"{cdata:.6f}"
    if isinstance(cdata, ctypes._Pointer) and False:
        value = ctypes.cast(cdata, ctypes.c_void_p).value
        return f'0x{value:X}'
    else:
        return repr(cdata)

class EmbFieldType:

    """
    Class for holding information about the fields
    """

    # noinspection PyProtectedMember
    def __init__(self, ctype, _cls, _type, bit_size: int, bit_offset: int):
        if isinstance(_type, str):
            g = sys.modules[_cls.__module__].__dict__
            if _type not in g and _type.endswith('_p'):
                # try resolving the non-pointer type
                _type = _type[:-len('_p')]
                if _type == _cls.__name__:
                    _type = ctypes.POINTER(ctype)
                else:
                    _type = g[_type]
            else:
                _type = g[_type]
        if not isinstance(_type, type):
            brk()
        self.base_cls = _cls
        self.base_type = _type
        self.bit_size = bit_size
        self.bit_offset = bit_offset

    @property
    def is_bitfield(self) -> bool:
        return self.bit_size is not None

    @property
    def is_ctypes_type(self) -> bool:
        # noinspection PyProtectedMember
        if not isinstance(self.base_type, type):
            raise TypeError("Base type isn't a type!")
        return issubclass(
            self.base_type, (ctypes._Pointer, ctypes._SimpleCData, ctypes.Structure, ctypes.Union, ctypes.Array)
        )

    @property
    def is_ctypes_simple_type(self):
        # noinspection PyProtectedMember
        return issubclass(self.base_type, ctypes._SimpleCData)

    @property
    def is_structure(self):
        return issubclass(self.base_type, ctypes.Structure)

    def elem_type(self):
        return c_elem_type(self.base_type)

    @property
    def is_array(self):
        return issubclass(self.base_type, ctypes.Array)

    @property
    def array_length(self):
        if self.is_array:
            return self.base_type._length_
        else:
            return 0


class _EmbTarget(Enum):
    """
    Target type for emb class creation
    """
    STRUCT = auto()
    UNION = auto()


def _check_value_for_type(field_type: EmbFieldType, value: Any):
    """
    Checks whether a value can be assigned to a field.

    :param field_type: type class of the field.
    :param value: value to be written
    :raises: ValueError
    """
    if field_type.is_array or field_type.is_ctypes_simple_type:
        # check for ctypes types, that have the _type_ attribute, containing a struct char.
        struct_char = getattr(field_type.base_type, _CTYPES_TYPE_ATTR)
        if hasattr(struct_char, _CTYPES_TYPE_ATTR):
            struct_char = field_type.base_type._length_ * getattr(struct_char, _CTYPES_TYPE_ATTR)
        is_signed = struct_char.islower()
        # noinspection PyProtectedMember
        if isinstance(value, ctypes._SimpleCData):
            _value = value.value
        else:
            _value = value
        try:
            if field_type.is_array:
                struct.pack(struct_char, *[x for x in _value])
            elif field_type.base_type == ctypes.c_char_p:
                # strings are ok
                pass
            else:
                struct.pack(struct_char, _value)
        except struct.error as e:
            brk()
            raise ValueError(
                f'{value} cannot be set for {field_type.base_type.__name__} ({repr(e)})!'
            ) from None
        if field_type.is_bitfield:
            if is_signed:
                max_raw = 2 ** (field_type.bit_size - 1) - 1
                min_raw = -(2 ** (field_type.bit_size - 1))
            else:
                max_raw = 2 ** field_type.bit_size - 1
                min_raw = 0
            if not min_raw <= _value <= max_raw:
                raise ValueError(f"Cannot set {_value} for this bitfield")
    else:
        brk()
        raise TypeError('Got non-ctypes type!')


def _is_little_endian(obj: ctypes.Structure) -> bool:
    """
    Checks whether a Structure instance/class is little endian

    :param obj: Structure instance/class
    :return: True if little endian
    """
    is_swapped = hasattr(obj, _CTYPES_SWAPPED_ATTR)
    if _SYS_ENDIANNESS_IS_LITTLE:
        return not is_swapped
    else:
        return is_swapped


def _is_emb_type(instance: EmbFieldType) -> bool:
    """
    Checks if an object/field is a emb instance by checking if it has the __emb_fields__ attribute

    :param instance: instance to check
    :return: True if emb instance
    """
    return hasattr(instance.base_type, _FIELDS)


# noinspection PyProtectedMember
def _short_type_name(typeobj: EmbFieldType) -> str:
    """
    Returns a short type name for a basic type, like u8, s16, etc...

    :param typeobj: emb type object
    :return: short name for the type
    """
    return c_short_type_name(typeobj.base_type)


# noinspection PyProtectedMember
def _c_type_name(typeobj: EmbFieldType) -> str:
    """
    Returns an ANSI c type name for a basic type, like unsigned char, signed short, etc...

    :param typeobj: emb type object
    :return: c type name for the type
    """
    # noinspection PyUnresolvedReferences
    # byte_size = struct.calcsize(typeobj.base_type_char)
    byte_size = c_elem_size(typeobj.base_type)
    if issubclass(typeobj.base_type, (ctypes.c_float, ctypes.c_double)):
        if byte_size == 4:
            return "float"
        elif byte_size == 8:
            return "double"
        else:
            raise ValueError("invalid length for float")
    else:
        if byte_size == 1:
            name = "char"
        elif byte_size == 2:
            name = "short"
        elif byte_size == 4:
            name = "int"
        elif byte_size == 8:
            name = "long"
        else:
            raise ValueError("invalid length")
        # noinspection PyUnresolvedReferences
        elem_type = c_elem_type(typeobj.base_type)
        signed = "signed" if elem_type.islower() else "unsigned"
        return f"{signed} {name}"


def __len_for_union(self):
    """
    Monkypatch __len__() method for ctypes.Union
    """
    return ctypes.sizeof(self)


def _print_field_value(field, typeobj):
    return c_repr(field)
    # if issubclass(typeobj.base_type, (ctypes.c_float, ctypes.c_double)):
    #     return f"{field:.6f}"
    # else:
    #     return f"0x{field:X}"


def __repr_for_union(self):
    """
    Monkypatch __repr__() method for ctypes.Union
    """
    _fields = getattr(self, _FIELDS)
    field_count = len(_fields)
    s = f'{self.__class__.__name__}('
    for i, (field_name, field_type) in enumerate(_fields.items()):
        _field = getattr(self, field_name)
        if _is_emb_type(field_type):
            s += f"{field_name}={repr(_field)}"
        else:
            if field_type.is_bitfield:
                bitfield_info = field_type.bit_size
            else:
                bitfield_info = ''
            s += f"{field_name}:{_short_type_name(field_type)}{bitfield_info}={_print_field_value(_field, field_type)}"
        if i < field_count - 1:
            s += ", "
    s += ')'
    return s


# Monkypatch ctypes.Union: it only works like this, because Union is a metaclass,
# and the method with exec/setattr does not work for it, as described here:
#   https://stackoverflow.com/questions/53563561/monkey-patching-class-derived-from-ctypes-union-doesnt-work
# However, it only seems to be needed for __len__ and __repr__.
ctypes.Union.__len__ = __len_for_union
ctypes.Union.__repr__ = __repr_for_union


def _add_method(
        cls: Type,
        name: str,
        args: Iterable[str],
        body: str,
        return_type: Any,
        docstring="",
        _globals: Optional[Dict[str, Any]] = None,
        _locals: Optional[Mapping] = None,
        class_method=False
):
    """
    Magic for adding methods dynamically to a class. Yes, it uses exec(). I know. Sorry about that.

    :param cls: class to extend
    :param name: name of the method to add
    :param args: arguments of the method
    :param body: body code of the method
    :param return_type: return type of the method
    :param docstring: optional docstring for the method
    :param _globals: globals for the method
    :param _locals: locals for the method
    :param class_method: if True, generates a classmethod
    """
    # default locals
    __locals = dict()
    __locals["_return_type"] = return_type
    return_annotation = "->_return_type"
    # default globals:
    __globals = {
        "cls": cls,
        "ctypes": ctypes,
        "struct": struct,
        "_is_emb_type": _is_emb_type,
        "_short_type_name": _short_type_name,
        "_c_type_name": _c_type_name,
        "_is_little_endian": _is_little_endian,
        "_check_value_for_type": _check_value_for_type,
        "_print_field_value": _print_field_value
    }
    # update globals and locals
    if _globals is not None:
        __globals.update(_globals)
    if _locals is not None:
        __locals.update(_locals)
    # final code
    args = ','.join(args)
    sig = f"def {name}({args}){return_annotation}"
    code = f"{sig}:\n{body}"
    # execute it and save to the class
    # with tempfile.TemporaryDirectory() as tmpdirname:
    if True:
        safesig = ''.join([(c if c.isalnum() else '_') for c in sig])
        filename = os.path.join(tempfolder, f'emb_dynamic_{safesig}.py')
        with open(filename, 'w') as f:
            f.write(code)
        expr = compile(code, filename, 'exec')
        try:
            exec(expr, __globals, __locals)
        except:
            brk()
            pm()
    method = __locals[name]
    method.__qualname__ = f'{cls.__qualname__}.{cls.__name__}.{method.__name__}'
    method.__doc__ = docstring
    if class_method:
        method = classmethod(method)
    setattr(cls, name, method)


def _generate_class(_cls, target: _EmbTarget, endian=sys.byteorder, pack=ctypes.sizeof(ctypes.c_size_t)):
    """
    Generates a new class based on the decorated one that we gen in the _cls parameter.
    Adds methods, sets bases, etc.

    :param _cls: class to work on
    :param target: union/struct
    :param endian: endianness for structures. Default is the system's byteorder.
    :param pack: packing for structures
    :return: generated class
    """
    # get the original class' annotations, we will parse these and generate the fields from these.
    cls_annotations = get_annotations(_cls, eval_str=True)

    # ctypes currently does not implement the BigEndianUnion and LittleEndianUnion despite its documentation
    # sais so. Therefore, we use simple Union for now. Details:
    # https://stackoverflow.com/questions/49524952/bigendianunion-is-not-part-of-pythons-ctypes
    # https://bugs.python.org/issue33178
    if endian == "little":
        _bases = {
            _EmbTarget.STRUCT: ctypes.LittleEndianStructure,
            _EmbTarget.UNION: ctypes.Union
        }
    elif endian == "big":
        _bases = {
            _EmbTarget.STRUCT: ctypes.BigEndianStructure,
            _EmbTarget.UNION: ctypes.Union
        }
    else:
        raise ValueError("Invalid endianness")

    # create the new class
    cls = type(_cls.__name__, (_bases[target], ), {})

    # set our special attribute to save fields
    setattr(cls, _FIELDS, {})
    _fields = getattr(cls, _FIELDS)

    # go through the annotations and create fields
    _ctypes_fields = []
    _first_endian = None
    _bitfield_counter = 0
    _bitfield_basetype_bitsize = 0
    _bitfield_basetype = None
    bit_offset = None
    for field_cnt, (field_name, _field_type) in enumerate(cls_annotations.items()):
        if isinstance(_field_type, tuple):
            __field_type, bit_size = _field_type
            if _bitfield_counter == 0:
                _bitfield_counter = bit_size
                _bitfield_basetype_bitsize = struct.calcsize(__field_type._type_) * 8
                _bitfield_basetype = __field_type
                bit_offset = 0
            else:
                _bitfield_counter += bit_size
                if __field_type != _bitfield_basetype:
                    raise SyntaxError("Bitfields must be of same type!")
                if _bitfield_counter > _bitfield_basetype_bitsize:
                    raise SyntaxError("Bitfield overflow!")
                if _bitfield_counter == _bitfield_basetype_bitsize:
                    # full bitfield
                    _bitfield_counter = 0
                    _bitfield_offset = 0
                    _bitfield_basetype_bitsize = 0
                    _bitfield_basetype = None
                bit_offset = (_bitfield_counter - bit_size)
        else:
            if _bitfield_counter > 0:
                raise SyntaxError("Incomplete bitfield definition!")
            __field_type = _field_type
            bit_size = None
        if field_cnt == len(cls_annotations) - 1:
            if _bitfield_counter > 0:
                raise SyntaxError("Incomplete bitfield definition!")
        field_type = EmbFieldType(ctype=cls, _cls=_cls, _type=__field_type, bit_size=bit_size, bit_offset=bit_offset)
        # noinspection PyProtectedMember
        if not field_type.is_ctypes_type:
            raise TypeError(
                f'Invalid type for field "{field_name}". Only ctypes types can be used!'
            )
        if target is _EmbTarget.UNION:
            # for unions, check if all sub-struct has the same endianness.
            if field_type.is_structure:
                if _first_endian is None:
                    _first_endian = _is_little_endian(field_type.base_type)
                else:
                    _endian = _is_little_endian(field_type.base_type)
                    if _endian != _first_endian:
                        raise TypeError('Only the same endianness is supported in a Union!')
        # save the field to our special attribute, and also for the ctypes _fields_ attribute
        _fields[field_name] = field_type
        if bit_size is None:
            _ctypes_fields.append((field_name, field_type.base_type))
        else:
            _ctypes_fields.append((field_name, field_type.base_type, bit_size))

    # set the ctypes special attributes, note, _pack_ must be set before _fields_!
    setattr(cls, _CTYPES_PACK_ATTR, pack)
    setattr(cls, _CTYPES_FIELDS_ATTR, _ctypes_fields)
    # save the endianness to us, because union streaming/building will need this
    setattr(cls, _ENDIAN, endian)

    # Add the generated methods

    # ---------------------------------------------------
    #           __init__
    # ---------------------------------------------------
    docstring = "init method for the class"
    body = f"""
        fields = getattr(self, '{_FIELDS}')
        if args:
            if kwargs:
                raise TypeError('Either positional arguments, or keyword arguments must be given!')
            if len(args) == len(fields):
                for arg_val, field_name in zip(args, fields):
                    setattr(self, field_name, arg_val)
            else:
                raise TypeError('Invalid number of arguments!')
        if kwargs:
            if args:
                raise TypeError('Either positional arguments, or keyword arguments must be given!')
            if len(kwargs) == len(fields):
                for field_name in fields:
                    try:
                        arg_val = kwargs[field_name]
                    except KeyError:
                        raise TypeError(f'Keyword argument {{field_name}} not specified!')
                    setattr(self, field_name, arg_val)
            else:
                raise TypeError('Invalid number of keyword arguments!')
    """
    _add_method(
        cls=cls,
        name="__init__",
        args=('self', '*args', '**kwargs',),
        body=body,
        docstring=docstring,
        return_type=None
    )

    # ---------------------------------------------------
    #           __len__
    # ---------------------------------------------------
    docstring = "Gets the byte length of the structure/union"
    body = f"""
        return ctypes.sizeof(self)
    """
    _add_method(
        cls=cls,
        name="__len__",
        args=('self',),
        body=body,
        docstring=docstring,
        return_type=int
    )

    # ---------------------------------------------------
    #           stream()
    # ---------------------------------------------------
    docstring = "gets the bytestream of the instance"
    if issubclass(cls, ctypes.Union):
        body = f"""
            if cls.__emb_endian__ == sys.byteorder:
                return bytes(self)
            else:
                _bytearray = bytearray(self)
                _bytearray.reverse()
                return bytes(_bytearray)
        """
    else:
        body = f"""
            return bytes(self)
        """
    _add_method(
        cls=cls,
        name="stream",
        args=('self',),
        body=body,
        docstring=docstring,
        return_type=bytes,
        _globals={"sys": sys}
    )

    # ---------------------------------------------------
    #           parse()
    # ---------------------------------------------------
    docstring = "parses the instance values from a bytestream"
    body = f"""
        if not isinstance(stream, bytes):
            raise TypeError("bytes required")
        ctypes.memmove(ctypes.addressof(self), stream, len(stream))
    """
    _add_method(
        cls=cls,
        name="parse",
        args=("self", "stream"),
        body=body,
        docstring=docstring,
        return_type=None
    )

    # ---------------------------------------------------
    #           ccode()
    # ---------------------------------------------------
    docstring = "Generates the c representation of the instance. Returns a list of the c code lines."
    body = f"""
        code = []
        _typename = 'struct' if issubclass(cls, ctypes.Structure) else 'union'
        code.append(f"typedef {{_typename}} _tag_{{cls.__name__}} {{{{")
        print(' ')
        for field_name, field_type in cls.{_FIELDS}.items():
            _field = getattr(cls, field_name)
            if _is_emb_type(field_type):
                subcode = field_type.base_type.ccode()
                code = subcode + code
                code.append(f"    {{field_type.base_type.__name__}} {{field_name}};")
            else:
                if field_type.is_bitfield:
                    code.append(f"    {{_c_type_name(field_type)}} {{field_name}} : {{field_type.bit_size}};")
                else:
                    code.append(f"    {{_c_type_name(field_type)}} {{field_name}};")
        code.append(f"}}}} {{cls.__name__}};")
        return code
    """
    _add_method(
        cls=cls,
        name="ccode",
        args=("cls",),
        body=body,
        docstring=docstring,
        return_type=Iterable[str],
        class_method=True
    )

    # ---------------------------------------------------
    #           print_ccode()
    # ---------------------------------------------------
    docstring = "Generates the c representation of the instance and prints it to the stdout."
    body = """
        print('\\n'.join(cls.ccode()))
    """
    _add_method(
        cls=cls,
        name="print_ccode",
        args=("cls",),
        body=body,
        docstring=docstring,
        return_type=None,
        class_method=True
    )

    # ---------------------------------------------------
    #           __repr__
    # ---------------------------------------------------
    docstring = "repr method for the instance"
    body = f"""
        field_count = len(self.{_FIELDS})
        s = f'{{cls.__name__}}('
        for i, (field_name, field_type) in enumerate(self.{_FIELDS}.items()):
            _field = getattr(self, field_name)
            if _is_emb_type(field_type):
                s += f'{{field_name}}={{repr(_field)}}'
            else:                
                if field_type.is_bitfield:
                    bitfield_info = f"@{{field_type.bit_size}}"
                else:
                    bitfield_info = ''
                s += f'{{field_name}}:{{_short_type_name(field_type)}}{{bitfield_info}}={{_print_field_value(_field, field_type)}}'
            if i < field_count - 1:
                s += ', ' 
        s += ')'
        return s
    """
    _add_method(
        cls=cls,
        name="__repr__",
        args=('self',),
        body=body,
        docstring=docstring,
        return_type=str
    )

    # ---------------------------------------------------
    #           __setattr__
    # ---------------------------------------------------
    docstring = "Attribute setter. Checks values."
    body = f"""
        field = self.__getattribute__(field_name)
        field_type = self.{_FIELDS}[field_name]
        if _is_emb_type(field_type):
            if not isinstance(field_type.base_type, type):
                import pdb, sys; mypdb = pdb.Pdb(stdout=sys.__stderr__); mypdb.set_trace()
            if not isinstance(value, field_type.base_type):
                raise TypeError(
                    f'invalid value for field "{{field_name}}"! Must be of type {{field_type}}!'
                )
            super(cls, self).__setattr__(field_name, value)
        else:
            _check_value_for_type(field_type, value)
            if isinstance(value, ctypes.Array):
                value = tuple([x for x in value])
            elif isinstance(value, ctypes._SimpleCData):
                if isinstance(value, ctypes.c_char_p):
                    #value = value.value.decode('utf-8')
                    value = value.value
                else:
                    value = value.value
            # import pdb, sys; mypdb = pdb.Pdb(stdout=sys.__stderr__); mypdb.set_trace()
            super(cls, self).__setattr__(field_name, value)
            # print('ok', file=sys.stderr)
    """
    _add_method(
        cls=cls,
        name="__setattr__",
        args=('self', 'field_name', 'value',),
        body=body,
        docstring=docstring,
        return_type=None
    )

    return cls


def emb_struct(_cls=None, *, endian=sys.byteorder, pack: int = ctypes.sizeof(ctypes.c_size_t)):
    """
    Magic decorator to create a user-friendly struct class

    :param _cls: used for distinguishing between call modes (with or without parens)
    :param endian: endianness. "little" or "big"
    :param pack: packing of the fields.
    :return:
    """
    def wrap(cls):
        return _generate_class(cls, _EmbTarget.STRUCT, endian, pack)
    if _cls is None:
        # call with parens: @emb_struct(...)
        return wrap
    else:
        # call without parens: @emb_struct
        return wrap(_cls)


def emb_union(_cls=None, *, endian=sys.byteorder, pack=ctypes.sizeof(ctypes.c_size_t)):
    """
    Magic decorator to create a user-friendly union class

    :param _cls: used for distinguishing between call modes (with or without parens)
    :param endian: endianness. "little" or "big"
    :return: decorated class
    """
    if endian != sys.byteorder:
        raise NotImplementedError(
            f"{endian} endian byteorder is currently not supported for Unions."
            f"This is because ctypes does not implement the BigEndianUnion and LittleEndianUnion despite its "
            f"documentation says so. Details:"
            f"https://stackoverflow.com/questions/49524952/bigendianunion-is-not-part-of-pythons-ctypes, "
            f"https://bugs.python.org/issue33178"
        )

    def wrap(cls):
        return _generate_class(cls, _EmbTarget.UNION, endian, pack=pack)

    if _cls is None:
        # call with parens: @emb_struct(...)
        return wrap
    else:
        # call without parens: @emb_struct
        return wrap(_cls)
