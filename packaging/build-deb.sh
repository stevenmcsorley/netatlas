#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/netatlas" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" "$SOURCE/dist"
cp -R "$SOURCE/src" "$SOURCE/bin" "$STAGE/opt/netatlas/"
find "$STAGE/opt/netatlas" -type d -name __pycache__ -exec rm -r -- {} +
ln -s /opt/netatlas/bin/netatlas "$STAGE/usr/bin/netatlas"
ln -s /opt/netatlas/bin/netatlas-gui "$STAGE/usr/bin/netatlas-gui"
cp "$SOURCE/packaging/io.github.stevenmcsorley.NetAtlas.desktop" "$STAGE/usr/share/applications/"
cp "$SOURCE/src/netatlas/data/icons/io.github.stevenmcsorley.NetAtlas.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$SOURCE/packaging/control" "$STAGE/DEBIAN/control"
dpkg-deb --root-owner-group --build "$STAGE" "$SOURCE/dist/netatlas_0.1.0_all.deb"
echo "Built $SOURCE/dist/netatlas_0.1.0_all.deb"
