"""SCIP wire reading and the occurrence range join.

The reader is hand-written (see ``oxn/scip/wire.py`` for why), so it is tested two ways:
here against bytes constructed to exercise each shape, and in the oracle lane against an
index produced by the real ``scip-python``. Synthetic bytes prove the decoding; a real
artifact proves the field numbers.
"""

from __future__ import annotations

import pytest

from oxn.graph.builder import build_file
from oxn.graph.model import EdgeKind, Provenance, Resolution
from oxn.languages import get_parser
from oxn.profiles import get_profile
from oxn.scip.index import ROLE_DEFINITION, parse_index
from oxn.scip.join import join_document
from oxn.scip.wire import WireFormatError, iter_fields, read_packed_varints, read_varint

# ---- protobuf encoding helpers, so fixtures are readable ---------------------------------


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def tag(number: int, wire_type: int) -> bytes:
    return varint(number << 3 | wire_type)


def delimited(number: int, payload: bytes) -> bytes:
    return tag(number, 2) + varint(len(payload)) + payload


def scalar(number: int, value: int) -> bytes:
    return tag(number, 0) + varint(value)


def occurrence(symbol: str, span: list[int], roles: int = 0) -> bytes:
    packed = b"".join(varint(item) for item in span)
    return delimited(1, packed) + delimited(2, symbol.encode()) + scalar(3, roles)


def document(path: str, occurrences: list[bytes], symbols: list[bytes] = ()) -> bytes:
    body = delimited(1, path.encode())
    body += b"".join(delimited(2, item) for item in occurrences)
    body += b"".join(delimited(3, item) for item in symbols)
    return body


# ---- wire format --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 2**21, 2**35])
def test_varints_round_trip(value: int) -> None:
    assert read_varint(varint(value), 0) == (value, len(varint(value)))


def test_truncated_varint_is_an_error() -> None:
    with pytest.raises(WireFormatError):
        read_varint(b"\x80\x80", 0)


def test_overrunning_length_is_an_error() -> None:
    with pytest.raises(WireFormatError):
        list(iter_fields(tag(1, 2) + varint(50) + b"short"))


def test_packed_varints() -> None:
    assert read_packed_varints(varint(3) + varint(5) + varint(300)) == [3, 5, 300]


def test_unknown_fields_are_skipped_by_wire_type() -> None:
    """Forward compatibility: a future SCIP field must not break the reader."""
    data = delimited(2, document("a.py", [occurrence("sym", [1, 2, 3])]))
    data += delimited(99, b"a future field")  # unknown, length-delimited
    data += scalar(98, 12345)  # unknown, varint
    index = parse_index(data)
    assert index.documents[0].relative_path == "a.py"
    assert index.occurrence_count == 1


# ---- SCIP records --------------------------------------------------------------------------


def test_three_element_range_is_single_line() -> None:
    index = parse_index(delimited(2, document("a.py", [occurrence("s", [4, 2, 9])])))
    found = index.documents[0].occurrences[0]
    assert (found.start_line, found.start_char, found.end_line, found.end_char) == (4, 2, 4, 9)


def test_four_element_range_spans_lines() -> None:
    """Handling only one of the two forms silently drops most occurrences in real code."""
    index = parse_index(delimited(2, document("a.py", [occurrence("s", [4, 2, 7, 9])])))
    found = index.documents[0].occurrences[0]
    assert (found.start_line, found.start_char, found.end_line, found.end_char) == (4, 2, 7, 9)


def test_symbol_roles_is_a_bitfield() -> None:
    index = parse_index(
        delimited(2, document("a.py", [occurrence("s", [0, 0, 1], ROLE_DEFINITION)]))
    )
    assert index.documents[0].occurrences[0].is_definition
    assert list(index.documents[0].definitions())


def test_occurrence_without_a_symbol_is_dropped() -> None:
    index = parse_index(delimited(2, document("a.py", [delimited(1, varint(0))])))
    assert index.documents[0].occurrences == []


def test_relationships_carry_is_implementation() -> None:
    relationship = delimited(1, b"base#") + scalar(3, 1)
    symbol = delimited(1, b"derived#") + delimited(4, relationship) + delimited(6, b"Derived")
    index = parse_index(delimited(2, document("a.py", [], [symbol])))
    info = index.documents[0].symbols[0]
    assert info.display_name == "Derived"
    assert info.relationships[0].symbol == "base#"
    assert info.relationships[0].is_implementation


# ---- the range join --------------------------------------------------------------------------

SOURCE = """class Base:
    def save(self, item):
        return item


class Derived(Base):
    def save(self, item):
        return helper(item)


def run():
    obj = Derived()
    return obj.save(1)
"""


def joined(occurrences: list[bytes], symbols: list[bytes] = ()):
    profile = get_profile("python")
    data = SOURCE.encode()
    tree = get_parser("python").parse(data)
    parsed = build_file("m.py", data, profile, tree.root_node)
    index = parse_index(delimited(2, document("m.py", occurrences, symbols)))
    result = join_document(list(parsed.entities), profile, tree.root_node, index.documents[0])
    names = {entity.id: entity.qualified_name for entity in parsed.entities}
    return result, names


def test_definitions_match_entities_by_name_position() -> None:
    occurrences = [
        occurrence("m/Base#", [0, 6, 10], ROLE_DEFINITION),
        occurrence("m/Base#save().", [1, 8, 12], ROLE_DEFINITION),
    ]
    result, names = joined(occurrences)
    assert names[result.definitions["m/Base#"]] == "m.Base"
    assert names[result.definitions["m/Base#save()."]] == "m.Base.save"


def test_a_call_is_attributed_to_its_enclosing_function() -> None:
    occurrences = [
        occurrence("m/run().", [10, 4, 7], ROLE_DEFINITION),
        occurrence("m/helper().", [12, 15, 19]),  # `obj.save` -> the method name
    ]
    result, names = joined(occurrences)
    calls = [edge for edge in result.edges if edge.kind is EdgeKind.CALLS]
    assert calls
    assert names[calls[0].src_id] == "m.run"
    assert calls[0].provenance is Provenance.SCIP
    assert calls[0].resolution is Resolution.L2


def test_a_qualified_callee_resolves_on_its_last_name() -> None:
    """`obj.save(1)` must resolve on `save`, not on the local variable `obj`.

    Matching the callee's start position finds the receiver, which is usually a local -- an
    edge to the local instead of to the method.
    """
    occurrences = [
        occurrence("m/run().", [10, 4, 7], ROLE_DEFINITION),
        occurrence("local 0", [12, 11, 14]),  # `obj`
        occurrence("m/Derived#save().", [12, 15, 19]),  # `save`
    ]
    result, _ = joined(occurrences)
    targets = {edge.dst_ref for edge in result.edges if edge.kind is EdgeKind.CALLS}
    assert "m/Derived#save()." in targets
    assert "local 0" not in targets


def test_inheritance_and_overrides_come_from_relationships() -> None:
    occurrences = [
        occurrence("m/Derived#", [5, 6, 13], ROLE_DEFINITION),
        occurrence("m/Derived#save().", [6, 8, 12], ROLE_DEFINITION),
    ]
    symbols = [
        delimited(1, b"m/Derived#") + delimited(4, delimited(1, b"m/Base#") + scalar(3, 1)),
        delimited(1, b"m/Derived#save().")
        + delimited(4, delimited(1, b"m/Base#save().") + scalar(3, 1)),
    ]
    result, names = joined(occurrences, symbols)
    kinds = {(edge.kind, edge.dst_ref) for edge in result.edges}
    assert (EdgeKind.EXTENDS, "m/Base#") in kinds
    assert (EdgeKind.OVERRIDES, "m/Base#save().") in kinds


def test_coverage_is_reported_even_when_nothing_matches() -> None:
    result, _ = joined([])
    assert result.definition_sites > 0
    assert result.definition_coverage == 0.0
    assert result.call_sites > 0
    assert result.call_coverage == 0.0
