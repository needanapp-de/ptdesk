import json

import pytest

from ptdesk.render import merge
from ptdesk.render.label import BarcodeElement, ImageElement, Label, TextElement, render


def name_label() -> Label:
    return Label(
        elements=[
            TextElement(text="{Name}\n{Abteilung}"),
            BarcodeElement(data="{Personalnummer}", symbology="code128"),
        ],
        sample={"Name": "Erika Muster", "Abteilung": "Einkauf", "unbenutzt": "x"},
    )


def test_placeholders_and_columns_in_order_with_usages():
    assert merge.placeholders("Raum { Nr }, {Haus}") == ["Nr", "Haus"]
    cols = merge.columns(name_label())
    assert [c.name for c in cols] == ["Name", "Abteilung", "Personalnummer"]
    assert cols[0].usages == ["Text"]
    assert cols[2].usages == ["Code 128"]
    assert cols[2].hint == "Verwendet in: Code 128"


def test_fill_label_replaces_and_keeps_line_breaks():
    record = {"name": "Max Mustermann", "ABTEILUNG": "Lager", "Personalnummer": "4711"}
    filled, errors = merge.fill_label(name_label(), record)
    assert errors == []
    assert filled.elements[0].text == "Max Mustermann\nLager"
    assert filled.elements[1].data == "4711"
    # the template itself is untouched
    assert name_label().elements[0].text == "{Name}\n{Abteilung}"


def test_cell_with_line_break_becomes_second_line():
    label = Label(elements=[TextElement(text="{Adresse}")])
    filled, errors = merge.fill_label(label, {"Adresse": "Hauptstraße 1\n12345 Musterstadt"})
    assert errors == []
    assert filled.elements[0].text.splitlines() == ["Hauptstraße 1", "12345 Musterstadt"]


def test_fill_label_reports_missing_columns_and_invalid_values():
    _, errors = merge.fill_label(name_label(), {"Name": "x"})
    assert "Spalte „Abteilung“ fehlt in der Tabelle" in errors
    label = Label(elements=[BarcodeElement(data="{EAN}", symbology="ean13")])
    _, errors = merge.fill_label(label, {"EAN": "abc"})
    assert len(errors) == 1 and errors[0].startswith("Barcode: Ungültiger EAN-13")


def test_preview_keeps_unknown_placeholders_without_errors():
    label = name_label()
    filled, errors = merge.fill_label(label, label.sample, strict=False)
    assert errors == []
    assert filled.elements[0].text == "Erika Muster\nEinkauf"
    assert filled.elements[1].data == "{Personalnummer}"
    assert render(filled).getbbox() is not None


def test_barcode_and_mixed_text():
    label = Label(
        elements=[
            BarcodeElement(data="{Nr}", symbology="code128"),
            TextElement(text="Inventar {Nr}\n{Raum}"),
            ImageElement(),  # no text slots
        ]
    )
    assert merge.column_names(label) == ["Nr", "Raum"]
    filled, errors = merge.fill_label(label, {"Nr": "A-17", "Raum": "Küche"})
    assert [e for e in errors if "Bild" not in e] == []
    assert filled.elements[0].data == "A-17"
    assert filled.elements[1].text == "Inventar A-17\nKüche"


def test_unique_column_and_prune_sample():
    label = name_label()
    assert merge.unique_column(label, "Name") == "Name 2"
    assert merge.unique_column(label, "Neu") == "Neu"
    merge.prune_sample(label)
    assert set(label.sample) == {"Name", "Abteilung"}


@pytest.mark.parametrize("cell, expected", [
    ("", (3, None)), ("2", (2, None)), ("2,0", (2, None)), ("0", (0, None)),
])
def test_copies_of(cell, expected):
    assert merge.copies_of({"anzahl": cell}, 3) == expected


@pytest.mark.parametrize("cell", ["-1", "1,5", "viele", "1000"])
def test_copies_of_invalid(cell):
    copies, error = merge.copies_of({"Anzahl": cell}, 1)
    assert copies == 0 and error


def test_merge_rows_marks_example_and_errors():
    label = name_label()
    example = merge.example_values(label)
    rows = [
        {"Name": "Erika Muster", "Abteilung": "Einkauf", "Personalnummer": ""},
        {"Name": "Max", "Abteilung": "Lager", "Personalnummer": "4711", "Anzahl": "2"},
    ]
    merged = merge.merge_rows(label, rows, [2, 3], default_copies=1, example=example)
    assert merged[0].is_example and not merged[0].ok  # empty barcode
    assert merged[1].ok and merged[1].copies == 2 and not merged[1].is_example
    assert merged[1].line == 3


def test_sample_is_saved_with_the_template():
    label = name_label()
    data = json.loads(json.dumps(label.to_dict()))
    assert Label.from_dict(data).sample == label.sample
    assert "sample" not in Label().to_dict()
