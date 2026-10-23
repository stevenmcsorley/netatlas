#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/bin/netatlas" "$HOME/.local/bin/netatlas-gui"
rm -f "$HOME/.local/share/applications/io.github.stevenmcsorley.NetAtlas.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.stevenmcsorley.NetAtlas.svg"
rm -rf "$HOME/.local/share/netatlas"
echo 'NetAtlas removed. No user data or system services were installed.'
