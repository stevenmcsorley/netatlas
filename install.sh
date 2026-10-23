#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/netatlas"
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' || {
  echo 'Install dependencies: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1'
  exit 1
}
mkdir -p "$DEST" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp -R "$SOURCE/src" "$SOURCE/bin" "$DEST/"
install -m 644 "$SOURCE/src/netatlas/data/icons/io.github.stevenmcsorley.NetAtlas.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
ln -sfn "$DEST/bin/netatlas" "$HOME/.local/bin/netatlas"
ln -sfn "$DEST/bin/netatlas-gui" "$HOME/.local/bin/netatlas-gui"
install -m 644 "$SOURCE/packaging/io.github.stevenmcsorley.NetAtlas.desktop" "$HOME/.local/share/applications/"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
echo 'Installed. Launch NetAtlas from Applications or run ~/.local/bin/netatlas-gui'
