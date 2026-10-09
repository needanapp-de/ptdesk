"""Command line interface: scan, info, text, print, testpage, render, table, calibrate, diagnose."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from PIL import Image

from ptdesk import __version__
from ptdesk.config import Config, config_path
from ptdesk.models import DEFAULT_MODEL, PrinterModel, Tape
from ptdesk.protocol.client import PrinterError, PrintOptions, PTouchClient
from ptdesk.render import merge
from ptdesk.render.label import (
    Label,
    fitted_length,
    image_label,
    print_image,
    testpage_label,
    text_label,
)
from ptdesk.table import TABLE_SUFFIXES, TableError, read_table, write_template
from ptdesk.transport.base import TransportError
from ptdesk.transport.discovery import scan
from ptdesk.transport.rfcomm import RfcommTransport

MODEL = DEFAULT_MODEL


def parse_mm(text: str) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError("Wert in mm angeben, z. B. 40 oder 12,5") from None
    if not 0 <= value <= 1000:
        raise argparse.ArgumentTypeError("Wert außerhalb des sinnvollen Bereichs")
    return value


def parse_rows(text: str) -> set[int]:
    """Spreadsheet row numbers like "3-10,12"."""
    rows: set[int] = set()
    try:
        for part in text.replace(" ", "").split(","):
            first, _, last = part.partition("-")
            rows.update(range(int(first), int(last or first) + 1))
    except ValueError:
        raise argparse.ArgumentTypeError("Zeilen wie in der Tabelle angeben, z. B. 3-10,12") from None
    return rows


def _tape(width_mm: float, model: PrinterModel = MODEL) -> Tape:
    tape = model.tape(width_mm)
    if tape is None:
        widths = ", ".join(t.name for t in model.tapes)
        raise ValueError(f"Der {model.name} kann {width_mm:g} mm Band nicht bedrucken (möglich: {widths})")
    return tape


def _printable_mm(tape: Tape, model: PrinterModel = MODEL) -> float:
    return tape.printable_px / model.px_per_mm


# --- connection ----------------------------------------------------------------

async def _find_transport(address: str | None, timeout: float) -> RfcommTransport:
    if address:
        return RfcommTransport(address)
    config = Config.load()
    if config.printer_address:
        return RfcommTransport(config.printer_address, config.printer_name)
    print(f"Suche Drucker ({timeout:g} s) …", file=sys.stderr)
    printers = [p for p in await scan(timeout) if p.is_ptouch]
    if not printers:
        raise TransportError("Kein P-touch-Drucker gefunden. Ist er eingeschaltet und in der Nähe?")
    found = printers[0]
    print(f"Verwende {found.name} ({found.address})", file=sys.stderr)
    return RfcommTransport(found.address, found.name)


def _remember(transport: RfcommTransport) -> None:
    """Store the printer as last used one (GUI and CLI connect to it without searching)."""
    config = Config.load()
    if config.printer_address != transport.address:
        config.printer_address = transport.address
        config.printer_name = transport.name if transport.name != transport.address else ""
        config.save()


async def _connect(args: argparse.Namespace) -> PTouchClient:
    transport = await _find_transport(args.address, args.timeout)
    client = PTouchClient(transport)
    info = await client.connect()
    _remember(transport)
    if info.model is None:
        await client.disconnect()
        raise PrinterError(f"{info.model_name} wird von ptdesk nicht unterstützt")
    return client


# --- commands --------------------------------------------------------------------

async def cmd_scan(args: argparse.Namespace) -> int:
    printers = await scan(args.timeout)
    if not printers:
        print("Keine Drucker gefunden.")
        return 1
    for p in printers:
        note = "" if p.is_ptouch else "  (kein P-touch)"
        paired = " gekoppelt" if p.paired else ""
        print(f"{p.address}  {p.name}{paired}{note}")
    return 0


async def cmd_info(args: argparse.Namespace) -> int:
    client = await _connect(args)
    try:
        status = await client.request_status()
        info = client.info
    finally:
        await client.disconnect()
    assert info is not None
    print(f"Modell:   {info.model_name} (Serie 0x{info.series_code:02x}, Modell 0x{info.model_code:02x})")
    if status.tape_present:
        print(f"Band:     {status.media_width_mm} mm, {status.media_name}, "
              f"{status.tape_color_name} mit {status.text_color_name}er Schrift")
    else:
        print("Band:     keins eingelegt")
    print(f"Fehler:   {', '.join(status.errors) or 'keine'}")
    return 0


def _load_label(args: argparse.Namespace, tape_mm: float) -> Label:
    path = Path(args.file)
    tape = _tape(tape_mm)
    if path.suffix.lower() in TABLE_SUFFIXES:  # table created by ptdesk: contains its label
        table = read_table(path)
        if table.template is None:
            raise ValueError(
                f"{path.name} enthält keine Etikettenvorlage. Aufruf: ptdesk print VORLAGE.json --data {path.name}"
            )
        args.data = args.data or str(path)
        return Label.from_dict(table.template)
    if path.suffix.lower() == ".json":
        return Label.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return image_label(str(path), args.length or 50.0, tape_mm, _printable_mm(tape), _margin(args))


def _margin(args: argparse.Namespace) -> float:
    value = args.margin if getattr(args, "margin", None) is not None else Config.load().margin_mm
    return max(value, MODEL.min_margin_mm)


def _offsets(args: argparse.Namespace) -> tuple[float, float]:
    config = Config.load()
    x = config.offset_x_mm if args.offset_x is None else args.offset_x
    y = config.offset_y_mm if args.offset_y is None else args.offset_y
    return x, y


def _check_label(label: Label) -> None:
    if not MODEL.min_length_mm <= label.length_mm <= MODEL.max_length_mm:
        raise ValueError(
            f"Etikettenlänge {label.length_mm:g} mm, möglich sind {MODEL.min_length_mm:g} bis {MODEL.max_length_mm:g} mm"
        )
    if errors := [f"{e.TITLE}: {msg}" for e in label.elements if (msg := e.validate())]:
        raise ValueError("; ".join(errors))


def _apply_margin(label: Label, args: argparse.Namespace) -> None:
    """The margin decides both the blank ends of the rendered label and the printer's feed: keep them equal."""
    if getattr(args, "margin", None) is not None:
        label.margin_mm = args.margin
    label.margin_mm = max(label.margin_mm, MODEL.min_margin_mm)


def _image(label: Label, args: argparse.Namespace) -> Image.Image:
    label = fitted_length(label, MODEL.dpi, MODEL.min_length_mm, MODEL.max_length_mm)
    _check_label(label)
    return print_image(label, _tape(label.tape_mm).printable_px, MODEL.dpi, *_offsets(args))


def _jobs(args: argparse.Namespace, label: Label) -> list[tuple[Image.Image, int, int | None]]:
    """``(image, copies, table row)`` per label; one per table row with ``--data``."""
    copies = getattr(args, "copies", 1)
    data = getattr(args, "data", None)
    if not data:
        if merge.has_placeholders(label):  # without a table: the example values, like the app's preview
            label = merge.fill_label(label, label.sample, strict=False)[0]
        return [(_image(label, args), copies, None)]

    table = read_table(Path(data))
    if missing := merge.missing_columns(label, table.columns):
        raise ValueError(f"In der Tabelle fehlen die Spalten: {', '.join(missing)}")
    example = merge.example_values(Label.from_dict(table.template)) if table.template else {}
    rows = merge.merge_rows(label, table.rows, table.lines, copies, example)
    if args.rows:
        rows = [r for r in rows if r.line in args.rows]
    selected = []
    for row in rows:
        if row.is_example:
            print(f"Zeile {row.line}: Beispielzeile, wird übersprungen", file=sys.stderr)
        elif row.errors:
            print(f"Zeile {row.line}: {'; '.join(row.errors)}", file=sys.stderr)
        elif row.copies > 0:
            selected.append(row)
    if any(r.errors and not r.is_example for r in rows) and not args.skip_errors:
        raise ValueError("Die Tabelle enthält Fehler. Korrigieren oder mit --skip-errors nur die fehlerfreien Zeilen drucken.")
    if not selected:
        raise ValueError("Keine Zeile zu drucken.")
    return [(_image(r.label, args), r.copies, r.line) for r in selected]


async def _print_label(args: argparse.Namespace, label: Label | None, make: object = None) -> int:
    """Connect, take the tape width from the printer, build the label (``make(tape_mm)``) and print it."""
    client = await _connect(args)
    try:
        tape = client.tape()
        if tape is None:
            raise PrinterError("Kein passendes Band eingelegt")
        if label is None:
            assert callable(make)
            label = make(tape.width_mm)
        if label.tape_mm != tape.width_mm:
            if not args.force_tape:
                raise PrinterError(
                    f"Die Vorlage ist für {label.tape_mm:g} mm Band, eingelegt ist {tape.name}. "
                    "Mit --force-tape trotzdem auf das eingelegte Band drucken."
                )
            label.tape_mm = tape.width_mm
        _apply_margin(label, args)
        jobs = _jobs(args, label)
        if args.preview:
            jobs[0][0].save(args.preview)
            print(f"Vorschau gespeichert: {args.preview}", file=sys.stderr)
        config = Config.load()
        options = PrintOptions(
            margin_mm=label.margin_mm,
            feed_last=config.feed_last if args.feed is None else args.feed,
            cut_marks=args.cut_marks,
        )
        prefix = ""

        def progress(stage: str, fraction: float) -> None:
            text = "Übertragung" if stage == "transfer" else "Druck"
            print(f"\r{prefix}{text}: {fraction * 100:5.1f} %   ", end="", file=sys.stderr, flush=True)

        for i, (image, copies, line) in enumerate(jobs, start=1):
            prefix = f"[{i}/{len(jobs)}] Zeile {line}: " if line is not None else ""
            last = i == len(jobs)
            job_options = options if last else PrintOptions(options.margin_mm, False, options.cut_marks)
            await client.print_pages([image] * copies, tape.width_mm, job_options, progress=progress)
            if not last:
                await client.request_status()
        print(file=sys.stderr)
    finally:
        await client.disconnect()
    if not options.feed_last:
        print("Letztes Etikett: zweimal kurz die Ein-Taste drücken, dann abschneiden.", file=sys.stderr)
    print("Fertig.", file=sys.stderr)
    return 0


async def cmd_text(args: argparse.Namespace) -> int:
    lines = "\n".join(args.lines).replace("\\n", "\n")

    def make(tape_mm: float) -> Label:
        tape = _tape(tape_mm)
        label = text_label(lines, args.length or 50.0, tape_mm, _printable_mm(tape), _margin(args))
        label.auto_length = args.length is None
        label.elements[0].bold = args.bold
        if args.font:
            label.elements[0].font = args.font
        return label

    return await _print_label(args, None, make)


async def cmd_print(args: argparse.Namespace) -> int:
    if Path(args.file).suffix.lower() in (".json", *TABLE_SUFFIXES):
        return await _print_label(args, _load_label(args, MODEL.tapes[-1].width_mm))
    return await _print_label(args, None, lambda tape_mm: _load_label(args, tape_mm))


async def cmd_testpage(args: argparse.Namespace) -> int:
    def make(tape_mm: float) -> Label:
        return testpage_label(args.length or 40.0, tape_mm, _printable_mm(_tape(tape_mm)), _margin(args))

    return await _print_label(args, None, make)


async def cmd_render(args: argparse.Namespace) -> int:
    tape_mm = args.tape or Config.load().tape_mm
    if args.text:
        label = text_label(
            "\n".join(args.text).replace("\\n", "\n"), args.length or 50.0, tape_mm,
            _printable_mm(_tape(tape_mm)), _margin(args),
        )
        label.auto_length = args.length is None
    elif args.file:
        label = _load_label(args, tape_mm)
    else:
        label = testpage_label(args.length or 40.0, tape_mm, _printable_mm(_tape(tape_mm)), _margin(args))
    _apply_margin(label, args)
    output = Path(args.output)
    for image, _copies, line in _jobs(args, label):
        path = output if line is None else output.with_name(f"{output.stem}-{line:03d}{output.suffix}")
        image.save(path)
        print(f"{path}: {image.width} × {image.height} px ({image.width / MODEL.px_per_mm:.1f} mm bedruckt)")
    return 0


async def cmd_table(args: argparse.Namespace) -> int:
    path = Path(args.file)
    label = Label.from_dict(json.loads(path.read_text(encoding="utf-8")))
    output = Path(args.output) if args.output else path.with_name(f"{path.stem}-tabelle.xlsx")
    write_template(output, label, path.name)
    print(f"{output}: Spalten {', '.join(merge.column_names(label))}")
    return 0


async def cmd_diagnose(args: argparse.Namespace) -> int:
    """Show versions and check that Bluetooth is usable (also a smoke test for packaged builds)."""
    import platform

    import openpyxl  # fails here if the packaged build lacks it

    from ptdesk.render.fonts import font_families
    from ptdesk.transport.rfcomm import bluetooth_available

    if sys.platform.startswith("linux"):
        import dbus_fast

        backend = f"BlueZ über dbus-fast {getattr(dbus_fast, '__version__', '')}".strip()
    elif sys.platform == "win32":
        from winrt.windows.devices.bluetooth import BluetoothDevice  # noqa: F401

        backend = "WinRT"
    else:
        backend = "nicht unterstützt"
    print(f"ptdesk         {__version__}")
    print(f"Python         {platform.python_version()} ({platform.system()} {platform.release()})")
    print(f"Bluetooth      RFCOMM {'verfügbar' if bluetooth_available() else 'FEHLT'}, Suche: {backend}")
    print(f"Schriften      {len(font_families())} Familien")
    print(f"Tabellen       openpyxl {openpyxl.__version__}")
    print(f"Konfiguration  {config_path()}")
    if args.scan:
        printers = await scan(args.timeout)
        print(f"Suche          OK, {len(printers)} Gerät(e) gefunden")
    return 0 if bluetooth_available() else 1


async def cmd_calibrate(args: argparse.Namespace) -> int:
    config = Config.load()
    if args.offset_x is not None:
        config.offset_x_mm = args.offset_x
    if args.offset_y is not None:
        config.offset_y_mm = args.offset_y
    if args.offset_x is not None or args.offset_y is not None:
        config.save()
        print(f"Gespeichert in {config_path()}")
    print(f"Druckversatz: X {config.offset_x_mm:+.2f} mm (Richtung Etikettenende), Y {config.offset_y_mm:+.2f} mm (nach unten)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ptdesk", description="Brother P-touch CUBE (PT-P300BT) per Bluetooth ansteuern")
    parser.add_argument("--version", action="version", version=f"ptdesk {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="mehr Ausgaben (-vv: Statusmeldungen)")
    sub = parser.add_subparsers(dest="command", required=True)

    def connection_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("-a", "--address", help="Bluetooth-Adresse des Druckers (sonst zuletzt benutzter oder Suche)")
        p.add_argument("--timeout", type=float, default=8.0, help="Suchdauer in Sekunden")

    def label_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("-l", "--length", type=parse_mm, help="Etikettenlänge in mm (Text: sonst automatisch)")
        p.add_argument("-m", "--margin", type=parse_mm, help=f"Rand an Anfang und Ende in mm (mind. {MODEL.min_margin_mm:g})")
        offset_args(p)

    def print_args(p: argparse.ArgumentParser) -> None:
        connection_args(p)
        label_args(p)
        p.add_argument("-c", "--copies", type=int, default=1, choices=range(1, MODEL.max_copies + 1), metavar="N",
                       help="Anzahl Kopien")
        feed = p.add_mutually_exclusive_group()
        feed.add_argument("--feed", dest="feed", action="store_true", default=None,
                          help="letztes Etikett zum Abschneiden ausgeben (Standard)")
        feed.add_argument("--chain", dest="feed", action="store_false",
                          help="Kettendruck: spart etwa 25 mm Band, letztes Etikett per Ein-Taste vorschieben")
        p.add_argument("--cut-marks", action="store_true", help="Schnittmarken zwischen den Kopien drucken")
        p.add_argument("--force-tape", action="store_true", help="Vorlage auf abweichendes Band drucken")
        p.add_argument("--preview", help="Druckbild zusätzlich als PNG speichern")

    def data_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data", metavar="TABELLE", help="Seriendruck: ein Etikett pro Zeile (.xlsx oder .csv)")
        p.add_argument("--rows", type=parse_rows, metavar="3-10,12", help="nur diese Tabellenzeilen")
        p.add_argument("--skip-errors", action="store_true", help="fehlerhafte Tabellenzeilen auslassen")

    def offset_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--offset-x", type=float, metavar="MM",
                       help="Druckversatz Richtung Etikettenende in mm; Standard: gespeicherte Kalibrierung")
        p.add_argument("--offset-y", type=float, metavar="MM",
                       help="Druckversatz nach unten in mm; Standard: gespeicherte Kalibrierung")

    p = sub.add_parser("scan", help="Drucker in der Nähe suchen")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("info", help="Drucker, eingelegtes Band und Fehler anzeigen")
    connection_args(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("text", help="Text drucken, jedes Argument eine Zeile")
    p.add_argument("lines", nargs="+", metavar="ZEILE")
    p.add_argument("-b", "--bold", action="store_true", help="fett")
    p.add_argument("-f", "--font", help="Schriftfamilie")
    print_args(p)
    p.set_defaults(func=cmd_text, data=None)

    p = sub.add_parser("print", help="Bild (PNG/JPG), Vorlage (.json) oder Tabelle aus ptdesk (.xlsx) drucken")
    p.add_argument("file")
    print_args(p)
    data_args(p)
    p.set_defaults(func=cmd_print)

    p = sub.add_parser("testpage", help="Testetikett drucken")
    print_args(p)
    p.set_defaults(func=cmd_testpage, data=None)

    p = sub.add_parser("render", help="Druckbild nur als PNG speichern (ohne Drucker)")
    p.add_argument("file", nargs="?", help="Bild oder Vorlage; ohne Angabe: Testetikett")
    p.add_argument("--text", nargs="+", metavar="ZEILE", help="statt einer Datei: Text, jedes Argument eine Zeile")
    p.add_argument("-t", "--tape", type=parse_mm, help="Bandbreite in mm (Standard: zuletzt benutzte)")
    p.add_argument("-o", "--output", default="etikett.png")
    label_args(p)
    data_args(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("table", help="Excel-Tabelle mit den Spalten einer Vorlage für den Seriendruck erstellen")
    p.add_argument("file", help="Vorlage (.json) mit {Spalte}-Feldern")
    p.add_argument("-o", "--output", help="Zieldatei (.xlsx oder .csv), Standard: VORLAGE-tabelle.xlsx")
    p.set_defaults(func=cmd_table)

    p = sub.add_parser("calibrate", help="Druckversatz anzeigen oder dauerhaft speichern")
    offset_args(p)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("diagnose", help="Versionen und Bluetooth-Unterstützung prüfen")
    p.add_argument("--scan", action="store_true", help="zusätzlich kurz nach Geräten suchen")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_diagnose)

    return parser


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # old Windows consoles cannot print every character
    args = build_parser().parse_args(argv)
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    try:
        code = asyncio.run(args.func(args))
    except (PrinterError, TransportError, TableError, OSError, ValueError) as e:
        print(f"\nFehler: {e}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
