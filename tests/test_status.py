from ptdesk.models import PT_P300BT, model_by_code
from ptdesk.protocol.status import ParseError, StatusReader, StatusType, parse_status

# reply of a real PT-P300BT with 12 mm laminated tape (white, black text), no error
REAL_STATUS = bytes.fromhex(
    "80 20 42 30 72 30 00 00 00 00 0c 01 00 00 00 00 00 00 00 00 00 00 00 00 01 08 00 00 00 00 00 00"
)


def test_parse_real_status() -> None:
    s = parse_status(REAL_STATUS)
    assert model_by_code(s.series_code, s.model_code) is PT_P300BT
    assert s.media_width_mm == 12
    assert s.media_type == 0x01
    assert s.tape_present
    assert s.status_type == StatusType.REPLY
    assert not s.has_error and s.errors == []
    assert s.tape_color_name == "weiß"
    assert s.text_color_name == "schwarz"
    assert PT_P300BT.tape_by_status(s.media_width_mm).printable_px == 64


def test_errors() -> None:
    raw = bytearray(REAL_STATUS)
    raw[8], raw[9], raw[18] = 0x01, 0x10, 0x02
    s = parse_status(bytes(raw))
    assert s.has_error
    assert s.errors == ["Kein Band eingelegt", "Deckel offen"]


def test_rejects_garbage() -> None:
    for data in (b"", REAL_STATUS[:31], b"\x00" + REAL_STATUS[1:]):
        try:
            parse_status(data)
        except ParseError:
            continue
        raise AssertionError(data)


def test_reader_split_and_merged() -> None:
    reader = StatusReader()
    assert reader.feed(b"\x00\x00" + REAL_STATUS[:10]) == []
    assert reader.feed(REAL_STATUS[10:] + REAL_STATUS[:1]) == [REAL_STATUS]
    assert reader.feed(REAL_STATUS[1:] + REAL_STATUS) == [REAL_STATUS, REAL_STATUS]
