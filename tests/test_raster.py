import random

from PIL import Image, ImageOps

from ptdesk.models import PT_P300BT
from ptdesk.protocol import raster
from ptdesk.render.label import print_image, text_label


def test_packbits_roundtrip() -> None:
    rng = random.Random(1)
    samples = [b"", b"\x00", b"\x00" * 16, b"\xff" * 300, bytes(range(200)), b"ab" * 70 + b"\x00" * 3]
    samples += [bytes(rng.choice((0, 0, 255, rng.randrange(256))) for _ in range(rng.randrange(1, 64))) for _ in range(300)]
    for data in samples:
        assert raster.unpackbits(raster.packbits(data)) == data


def test_packbits_known_vector() -> None:
    # example from the TIFF 6.0 specification (section 9)
    unpacked = bytes.fromhex("AA AA AA 80 00 2A AA AA AA AA 80 00 2A 22 AA AA AA AA AA AA AA AA AA AA")
    assert raster.unpackbits(bytes.fromhex("FE AA 02 80 00 2A FD AA 03 80 00 2A 22 F7 AA")) == unpacked
    assert raster.unpackbits(raster.packbits(unpacked)) == unpacked


def test_raster_line_mapping() -> None:
    tape = PT_P300BT.tape(12)
    image = Image.new("1", (3, tape.printable_px), 1)
    image.putpixel((1, 0), 0)  # second column, top edge of the printable band
    image.putpixel((2, tape.printable_px - 1), 0)  # third column, bottom edge
    lines = raster.raster_lines(image, 128, tape.pin_offset)
    assert len(lines) == 3 and all(len(line) == 16 for line in lines)
    assert lines[0] == bytes(16)
    assert lines[1] == bytes(4) + b"\x80" + bytes(11)  # pin 32
    assert lines[2] == bytes(11) + b"\x01" + bytes(4)  # pin 95


def test_raster_line_commands() -> None:
    assert raster.raster_line(bytes(16)) == b"Z"
    line = bytes(4) + b"\xff" * 8 + bytes(4)
    cmd = raster.raster_line(line)
    assert cmd[:1] == b"G" and cmd[1] | cmd[2] << 8 == len(cmd) - 3
    assert raster.unpackbits(cmd[3:]) == line


def test_print_information_matches_official_app() -> None:
    # official app, 12 mm laminated tape, 264 raster lines (stecman's capture)
    assert raster.print_information(0x01, 12, 264, True) == bytes.fromhex("1B 69 7A C4 01 0C 00 08 01 00 00 00 00")
    assert raster.print_information(0x01, 12, 264, False)[-2:] == b"\x01\x00"
    assert raster.margin(28) == bytes.fromhex("1B 69 64 1C 00")


def test_encode_job_structure() -> None:
    tape = PT_P300BT.tape(12)
    label = text_label("Hallo", 40, 12, tape.printable_px / PT_P300BT.px_per_mm)
    image = print_image(label, tape.printable_px)
    settings = raster.JobSettings(0x01, 12, 128, tape.pin_offset, 18)
    job = raster.encode_job([image, image], settings)
    assert job.startswith(bytes(64) + b"\x1b@\x1bia\x01\x1biz")
    assert job.endswith(b"\x1a")
    assert job.count(b"\x1biz") == 2 and job.count(b"\x0c") >= 1
    first = job.index(b"\x1biz")
    assert job[first + 3 : first + 13] == raster.print_information(0x01, 12, image.width, True)[3:]


def test_print_image_size() -> None:
    tape = PT_P300BT.tape(12)
    label = text_label("Zeile 1\nZeile 2", 50, 12, tape.printable_px / PT_P300BT.px_per_mm, margin_mm=2.5)
    image = print_image(label, tape.printable_px)
    assert image.height == 64
    assert image.width == round(50 * 180 / 25.4) - 2 * round(2.5 * 180 / 25.4)


def test_change_tape_scales_band() -> None:
    from ptdesk.render.label import change_tape

    mm12 = PT_P300BT.tape(12).printable_px / PT_P300BT.px_per_mm
    mm9 = PT_P300BT.tape(9).printable_px / PT_P300BT.px_per_mm
    label = text_label("Hallo", 40, 12, mm12)
    change_tape(label, 9, mm12, mm9)
    e = label.elements[0]
    assert label.tape_mm == 9
    assert abs(e.y - (9 - mm9) / 2) < 0.01 and abs(e.height - mm9) < 0.01
    image = print_image(label, PT_P300BT.tape(9).printable_px)
    assert image.height == 50
    assert ImageOps.invert(image.convert("L")).getbbox() is not None  # text is still printed
