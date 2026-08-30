"""A minimal protobuf wire-format reader for SCIP.

**Why hand-rolled.** OXN needs six fields from a stable, versioned schema. The alternative
is the ``protobuf`` runtime plus a vendored generated module and a codegen step in the
build, for an *optional* feature (ADR-0001 weighs install cost, not purity). If the schema
ever grows past what this reads comfortably, ``protobuf`` -- free and Apache-2.0 -- is the
fallback, and swapping to it changes only this module.

**Forward compatibility.** Only the field numbers OXN consumes are decoded; every other
field is skipped *by wire type*, which is exactly what makes an unknown future field
harmless rather than fatal.

The format itself: a message is a sequence of ``(key, value)`` pairs where the key is a
varint holding ``field_number << 3 | wire_type``. Wire type 0 is a varint, 2 is
length-delimited, 1 and 5 are fixed 64- and 32-bit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

VARINT = 0
FIXED64 = 1
LENGTH_DELIMITED = 2
FIXED32 = 5


class WireFormatError(ValueError):
    """Raised when the bytes are not a well-formed protobuf message."""


def read_varint(buffer: bytes, offset: int) -> tuple[int, int]:
    """Decode a base-128 varint. Returns ``(value, next offset)``."""
    result = 0
    shift = 0
    while True:
        if offset >= len(buffer):
            raise WireFormatError("truncated varint")
        byte = buffer[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 70:
            raise WireFormatError("varint too long")


def iter_fields(buffer: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """Yield ``(field number, wire type, value)`` for every field in a message.

    Length-delimited fields yield their raw bytes; varints yield an int; fixed-width fields
    yield their raw bytes. Unknown fields are yielded too -- the caller ignores what it does
    not recognise, which is where forward compatibility comes from.
    """
    offset = 0
    length = len(buffer)
    while offset < length:
        key, offset = read_varint(buffer, offset)
        number, wire_type = key >> 3, key & 0x07

        if wire_type == VARINT:
            value, offset = read_varint(buffer, offset)
            yield number, wire_type, value
        elif wire_type == LENGTH_DELIMITED:
            size, offset = read_varint(buffer, offset)
            end = offset + size
            if end > length:
                raise WireFormatError("length-delimited field overruns the message")
            yield number, wire_type, buffer[offset:end]
            offset = end
        elif wire_type == FIXED64:
            yield number, wire_type, buffer[offset : offset + 8]
            offset += 8
        elif wire_type == FIXED32:
            yield number, wire_type, buffer[offset : offset + 4]
            offset += 4
        else:
            raise WireFormatError(f"unsupported wire type {wire_type}")


def read_packed_varints(buffer: bytes) -> list[int]:
    """Decode a packed repeated varint field, as SCIP uses for ranges."""
    values: list[int] = []
    offset = 0
    while offset < len(buffer):
        value, offset = read_varint(buffer, offset)
        values.append(value)
    return values


def text(value: bytes | int) -> str:
    """A length-delimited field as UTF-8, replacing anything invalid."""
    if isinstance(value, int):  # pragma: no cover - schema mismatch
        return str(value)
    return value.decode("utf-8", "replace")
