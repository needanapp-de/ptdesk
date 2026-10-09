# ptdesk

Desktop-App für den Etikettendrucker **Brother P-touch CUBE PT-P300BT** unter **Linux und Windows**. Die
Verbindung läuft per **Bluetooth**. Damit lassen sich Bandetiketten mit Text, Barcodes, Bildern und Rahmen gestalten
und drucken, ohne die Handy-App.

- Echte 1-Bit-Vorschau bei 180 dpi, also genau das, was gedruckt wird
- **Mehrzeiliger Text**, der automatisch so groß wird, wie die Bandbreite erlaubt
- **Länge automatisch**: das Etikett wird so lang wie der Text
- Erkennt das eingelegte Band (3,5 / 6 / 9 / 12 mm, Farbe) und passt das Etikett an
- **Seriendruck aus Excel:** ein Etikett pro Tabellenzeile, die passende Tabelle erstellt ptdesk selbst
- Kommandozeile für Skripte und Automatisierung

Getestet mit PT-P300BT und 12-mm-TZe-Band (weiß/schwarz) unter Ubuntu 26.04, auch mehrzeiliger Text, Kopien und
Schnittmarken. **Noch nicht am Gerät getestet:** die Windows-Version und der Seriendruck aus einer Tabelle.

> Inoffizielles Projekt, nicht mit Brother verbunden. Brother und P-touch sind Marken der Brother Industries, Ltd.

## Installation

### Fertiges Paket

| System  | Datei                          | Start                           |
|---------|--------------------------------|---------------------------------|
| Linux   | `ptdesk-linux-x86_64.tar.gz`   | entpacken, `ptdesk/ptdesk`      |
| Windows | `ptdesk-windows.zip`           | entpacken, `ptdesk\ptdesk.exe`  |

Unter Linux trägt `packaging/install-linux.sh` die App ins Anwendungsmenü ein und legt den Befehl `ptdesk`
in `~/.local/bin` an.

### Aus dem Quellcode (Python ≥ 3.11)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ptdesk-gui
```

Unter Windows heißen die Pfade `.venv\Scripts\...`.

## Drucker verbinden

1. PT-P300BT einschalten.
2. **Ist er mit dem Handy verbunden, dort trennen**, sonst kann der PC ihn nicht erreichen.
3. In ptdesk auf **„Drucker verbinden …“** klicken und den Eintrag `PT-P300BT…` wählen.

- **Linux:** Koppeln ist nicht nötig. Die erste Verbindung dauert etwa 10 Sekunden, danach geht es schneller.
- **Windows:** Falls er in der Suche fehlt oder die Verbindung scheitert, den Drucker zuerst in den
  Bluetooth-Einstellungen koppeln („Gerät hinzufügen“ → Bluetooth). Unter Windows ist das noch ungetestet.

Ab dann verbindet sich ptdesk beim Start automatisch mit diesem Drucker. Der PT-P300BT schaltet sich nach
einigen Minuten ohne Druck selbst ab; danach einfach wieder einschalten und neu verbinden.

## Bedienung

- **Links:** Band und Länge, Elemente hinzufügen, Seriendruck, Kopien und Drucken.
- **Mitte:** Vorschau, das Band läuft waagerecht.
  - Elemente anklicken und ziehen, mit dem blauen Eckpunkt die Größe ändern.
  - Pfeiltasten verschieben um 0,5 mm (mit Shift um 0,1 mm), Entf löscht.
  - Die rot schraffierten Streifen oben und unten erreicht der Druckkopf nicht. Auf 12-mm-Band sind etwa
    9 mm bedruckbar.
  - Die grau schraffierten Enden sind der Rand, dort schiebt der Drucker nur Band vor.
- **Rechts:** Eigenschaften des gewählten Elements.
  - Text: mehrere Zeilen mit Enter, Schrift, Größe, fett, Ausrichtung, „verkleinern, bis Text ins Feld passt“.
  - Barcode: Code 128, EAN-13, EAN-8, Code 39.
  - Bild: gerastert oder mit Schwellwert.
- **Datei-Menü:** Vorlagen (`.json`) speichern und öffnen. Bilder werden in die Vorlage eingebettet. Export als PNG.

### Band und Länge

- Ist ein Drucker verbunden, kommt die **Bandbreite vom Drucker**. Wird ein anderes Band eingelegt, passt ptdesk
  die Elemente an den neuen druckbaren Bereich an.
- **„Länge an den Text anpassen“:** Texte werden so groß, wie die Bandbreite erlaubt, und das Etikett so lang
  wie nötig. Kurze Etiketten werden auf die Mindestlänge von 25 mm verlängert und mittig gesetzt.
- **Rand:** unbedruckter Vorschub am Anfang und am Ende, mindestens 2,4 mm.

### Drucken

- **„Letztes Etikett ausgeben“ (Standard):** Das letzte Etikett wird bis zum Messer vorgeschoben und kann gleich
  mit dem Hebel abgeschnitten werden. Dabei entstehen pro Druck etwa 25 mm Leerband, weil das Messer vor dem
  Druckkopf sitzt.
- **Ausgeschaltet (Kettendruck):** spart dieses Leerband. Das letzte Etikett bleibt im Drucker, bis das nächste
  gedruckt wird oder du die Ein-Taste zweimal kurz drückst.
- **Kopien** werden aneinanderhängend gedruckt. Mit „Schnittmarken zwischen den Kopien“ markiert der Drucker die
  Schnittstellen.

### Seriendruck aus einer Excel-Tabelle

Ein Etikett pro Tabellenzeile, z. B. Namensschilder, Inventarnummern oder Kabelbeschriftungen.

1. **Felder an die Tabelle binden:** beim Text oder Barcode „Tabellenspalte einfügen“ wählen oder `{Spaltenname}`
   direkt eintippen. Gemischt geht auch, z. B. `{Name}` in der ersten und `Raum {Raum}` in der zweiten Zeile.
2. **Seriendruck → Tabelle erstellen …** speichert eine Excel-Datei (`.xlsx`, wahlweise CSV) mit einer Spalte pro
   Feld, Zeile 2 als Beispiel und der Spalte „Anzahl“ (wie oft die Zeile gedruckt wird, leer = Kopien aus ptdesk,
   0 = überspringen).
3. Die Tabelle in Excel oder LibreOffice ausfüllen und speichern. Ein Zeilenumbruch in einer Zelle (Alt+Enter)
   wird auf dem Etikett zu einer neuen Zeile.
4. **Seriendruck → Tabelle öffnen …** zeigt die Zeilen unter der Vorschau. Ein Klick auf eine Zeile zeigt ihr
   Etikett. Fehlerhafte Zeilen sind rot markiert und werden nicht gedruckt.
5. **„N Etiketten drucken“** druckt die angehakten Zeilen nacheinander als zusammenhängende Kette. Bricht der Druck
   ab, sind die schon gedruckten Zeilen markiert; erneutes Drucken macht mit den übrigen weiter.

Mit „Länge an den Text anpassen“ bekommt jede Zeile ihre eigene, passende Länge.

## Kommandozeile

```bash
ptdesk scan                                   # Drucker in der Nähe suchen
ptdesk info                                   # Modell, eingelegtes Band, Fehler
ptdesk text "Zeile 1" "Zeile 2"               # zweizeiligen Text drucken, Länge automatisch
ptdesk text -l 60 -b "Kabel 17"               # 60 mm lang, fett
ptdesk testpage                               # Testetikett
ptdesk print logo.png -l 40                   # Bild drucken
ptdesk print vorlage.json -c 3                # Vorlage aus der App drucken (3 Kopien)
ptdesk table vorlage.json                     # Excel-Tabelle für den Seriendruck erstellen
ptdesk print vorlage-tabelle.xlsx             # Seriendruck: ein Etikett pro Tabellenzeile
ptdesk render --text "Probe" -t 9 -o p.png    # nur rendern, ohne Drucker
ptdesk calibrate --offset-y 0.2               # Druckversatz dauerhaft speichern
```

| Option | Bedeutung |
|---|---|
| `-a ADRESSE` | Drucker direkt wählen statt zuletzt benutztem oder Suche |
| `-l MM` | Etikettenlänge (bei `text` sonst automatisch) |
| `-m MM` | Rand an Anfang und Ende |
| `-c N` | Kopien |
| `--chain` | Kettendruck, spart etwa 25 mm Band (letztes Etikett per Ein-Taste vorschieben) |
| `--cut-marks` | Schnittmarken zwischen den Kopien |
| `--preview datei.png` | Druckbild zusätzlich speichern |
| `--data TABELLE` | Seriendruck aus `.xlsx`/`.csv` (bei Tabellen aus ptdesk nicht nötig) |
| `--rows 3-10,12` | nur diese Tabellenzeilen |
| `--skip-errors` | fehlerhafte Zeilen auslassen statt abzubrechen |

Die Einstellungen liegen in `~/.config/ptdesk/config.json` (Windows: `%APPDATA%\ptdesk\config.json`), das Protokoll
der App in `ptdesk.log` daneben.

## Selbst bauen

```bash
packaging/build-linux.sh
```

Unter Windows: `powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1`

## Lizenz

MIT, siehe [LICENSE](LICENSE). Quellen des Protokolls und Lizenzen der Abhängigkeiten stehen in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
