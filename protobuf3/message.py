from collections import namedtuple
from protobuf3.fields.base import BaseField
from protobuf3.wire_types import *

WireField = namedtuple('WireField', ['type', 'value'])


class Message(object):
    __fields = None

    @classmethod
    def __load_django_style_fields(cls):
        cls.__fields = {}
        for (field_name, field_object) in cls.__dict__.items():
            if isinstance(field_object, BaseField):
                cls.__fields[field_object.field_number] = field_object
                field_object.field_name = field_name

    def __new__(cls, *args, **kwargs):
        if not cls.__fields:
            cls.__load_django_style_fields()

        return super().__new__(cls)

    @classmethod
    def add_field(cls, field_name, field_instance):
        if not cls.__fields:
            cls.__load_django_style_fields()

        cls.__fields[field_instance.field_number] = field_instance
        field_instance.field_name = field_name

        setattr(cls, field_name, field_instance)

    def __init__(self):
        self.__read_offset = 0
        self.__wire_message = {}
        self.__parent = None

    def _decode_field_signature(self, data):
        number = self._decode_varint(data)

        field_type = number & 0b111
        field_number = number >> 3

        if field_type == FIELD_VARIABLE_LENGTH:
            field_length = self._decode_varint(data)
        elif field_type in (FIELD_START_GROUP, FIELD_END_GROUP):
            raise NotImplementedError("Groups is deprecated and unsupported in protobuf3")
        elif field_type in (FIELD_VARINT, FIELD_FIXED64, FIELD_FIXED32):
            field_length = None
        else:
            raise ValueError("Unknown wire type")

        return field_type, field_number, field_length

    @staticmethod
    def _encode_field_signature(field_type, field_number, field_length=None):
        if field_type not in (FIELD_VARINT, FIELD_FIXED64, FIELD_VARIABLE_LENGTH, FIELD_FIXED32):
            raise ValueError("Unknown field type for serialization")

        result = Message._encode_varint((field_number << 3) | field_type)

        if field_type == FIELD_VARIABLE_LENGTH:
            assert field_length is not None

            result += Message._encode_varint(field_length)

        return result

    def _decode_varint(self, data):
        # Not very pretty, but fast
        varint = 0
        offset = 0
        for offset, byte in enumerate(data[self.__read_offset:]):
            varint += (1 << 7) ** offset * (byte & 0b01111111)
            if not byte & (1 << 7):
                break

        # Adjust offset to the next byte
        self.__read_offset += offset + 1

        return varint

    @staticmethod
    def _encode_varint(number):
        result = []

        while number:
            next_byte = number % 128
            number //= 128

            if number:
                next_byte |= 1 << 7
            result.append(next_byte)
        
        if len(result) == 0:
            result = [0]

        return bytes(result)

    def _decode_raw_message(self, data):
        def __read_n_bytes(n):
            result = bytes(data[self.__read_offset:self.__read_offset + n])
            self.__read_offset += n
            return result

        while self.__read_offset < len(data):
            field_signature = self._decode_field_signature(data)
            field_type, field_number, field_length = field_signature
            field_object = self.__fields.get(field_number, BaseField(field_number))

            if field_type != field_object.WIRE_TYPE and field_object.WIRE_TYPE != -1:
                raise ValueError

            if field_type == FIELD_VARINT:
                field_value = self._decode_varint(data)
            elif field_type == FIELD_FIXED64:
                field_value = __read_n_bytes(8)
            elif field_type == FIELD_VARIABLE_LENGTH:
                field_value = __read_n_bytes(field_length)
            elif field_type == FIELD_FIXED32:
                field_value = __read_n_bytes(4)
            else:
                raise NotImplementedError

            wire_field = WireField(type=field_type, value=field_value)
            if field_number in self.__wire_message:
                if self.__wire_message[field_number][0].type != field_type:
                    raise ValueError

                self.__wire_message[field_number].append(wire_field)
            else:
                self.__wire_message[field_number] = [wire_field]

    def _check_required_fields(self):
        missing_fields = []

        for (field_number, field_object) in self.__fields.items():
            if field_object.required and field_number not in self.__wire_message:
                missing_fields.append(field_object.field_name)

        if missing_fields:
            raise KeyError("Some required fields are missing: " + ", ".join(missing_fields))

    def _get_wire_values(self, field_number):
        return self.__wire_message.get(field_number, [])

    def _set_parent(self, parent_msg):
        self.__parent = parent_msg

    def _set_wire_values(self, field_number, field_type, field_value, index=None, insert=False,
                         append=False, remove=False):
        if field_number not in self.__wire_message:
            self.__wire_message[field_number] = []
        wire_field = WireField(type=field_type, value=field_value)
        if append:
            self.__wire_message[field_number].append(wire_field)
        elif insert:
            self.__wire_message[field_number].insert(index, wire_field)
        elif remove:
            del self.__wire_message[field_number][index]
        elif index is not None:
            self.__wire_message[field_number][index] = wire_field
        else:
            self.__wire_message[field_number] = [wire_field]

        if self.__parent:
            msg, number, idx = self.__parent
            msg._set_wire_values(number, FIELD_VARIABLE_LENGTH, self.encode_to_bytes(), idx)

    def _drop_wire_value(self, field_number):
        del self.__wire_message[field_number]

    def parse_from_bytes(self, bytes_array):
        self.__wire_message = {}
        data = memoryview(bytes_array)
        self._decode_raw_message(data)

        self._check_required_fields()

    @classmethod
    def create_from_bytes(cls, bytes_array):
        tmp = cls()
        tmp.parse_from_bytes(bytes_array)
        return tmp

    def encode_to_bytes(self, ignore_unknown=False):
        self._check_required_fields()

        result = []

        for field_number in sorted(self.__wire_message.keys()):
            if ignore_unknown and field_number not in self.__fields:
                continue

            for it in self.__wire_message[field_number]:
                field_len = len(it.value) if it.type == FIELD_VARIABLE_LENGTH else None

                result.append(Message._encode_field_signature(it.type, field_number, field_len))

                if it.type == FIELD_VARINT:
                    result.append(Message._encode_varint(it.value))
                else:
                    result.append(it.value)

        return b''.join(result)

    def __contains__(self, item):
        field = self.__class__.__dict__.get(item)

        if field is None:
            return False

        return field.field_number in self.__wire_message

    def __repr__(self):
        result = ['<Message({})>'.format(self.__class__.__name__)]
        for field_number in self.__wire_message:
            if field_number in self.__fields:
                field = self.__fields[field_number]
                result.append('{}{}:'.format(' ' * 4, repr(field)))
                for it in self.__wire_message[field_number]:
                    value_repr = str(field._convert_to_final_type(it.value))
                    value_repr = ''.join(map(lambda x: ' ' * 8 + x, value_repr.split('\n')))
                    result.append(value_repr)
            else:
                result.append(
                    '{}Unknown field with id={} wire_type={}:'.format(' ' * 4, field_number,
                                                                      self.__wire_message[
                                                                          field_number][0].type))
                for it in self.__wire_message[field_number]:
                    result.append('{}{}'.format(' ' * 8, it.value))

        return '\n'.join(result)
