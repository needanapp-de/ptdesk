# Third-party notices

## Protocol sources

The P-touch raster protocol in `src/ptdesk/protocol/` is written from these sources; no code was copied:

- Brother "Software Developer's Manual, Raster Command Reference PT-E550W/P750W/P710BT" (v1.02):
  print data structure, command layouts, status reply.
- The command sequence of the official app for the PT-P300BT as recorded by stecman
  (https://gist.github.com/stecman/ee1fd9a8b1b6f0fdd170ee87ba2ddafd).
- Printer and tape geometry from the printer definition of Brother's "P-touch Design&Print 2" app.

Brother and P-touch are trademarks of Brother Industries, Ltd. ptdesk is not affiliated with Brother.

## DejaVu Sans

`src/ptdesk/fonts/DejaVuSans*.ttf`, license in `src/ptdesk/fonts/LICENSE-DejaVu.txt`
(Bitstream Vera license, DejaVu changes are public domain).

## Runtime dependencies

| Package | License |
|---|---|
| PySide6 / Qt | LGPL-3.0 |
| Pillow | MIT-CMU (HPND) |
| python-barcode | MIT |
| openpyxl | MIT |
| et-xmlfile | MIT |
| dbus-fast (Linux) | MIT |
| winrt-* (Windows) | MIT |
