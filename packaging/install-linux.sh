#!/usr/bin/env bash
# Install the built app (dist/ptdesk) for the current user and add a menu entry.
set -euo pipefail
cd "$(dirname "$0")/.."

src="dist/ptdesk"
dest="$HOME/.local/share/ptdesk"
if [ ! -x "$src/ptdesk" ]; then
    echo "Zuerst packaging/build-linux.sh ausführen." >&2
    exit 1
fi

rm -rf "$dest"
mkdir -p "$dest" "$HOME/.local/bin" "$HOME/.local/share/applications"
cp -a "$src/." "$dest/"
ln -sf "$dest/ptdesk-cli" "$HOME/.local/bin/ptdesk"

cat > "$HOME/.local/share/applications/ptdesk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ptdesk
Comment=Etiketten für den Brother P-touch CUBE drucken
Exec=$dest/ptdesk
Icon=$dest/ptdesk.png
Terminal=false
Categories=Office;Utility;
EOF

echo "Installiert nach $dest"
echo "Startmenü: ptdesk   ·   Kommandozeile: ptdesk (in ~/.local/bin)"
