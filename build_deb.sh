#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

VERSION="0.1.5"
ARCH="all"
PACKAGE="revfur"
ROOT="build/deb-root"
OUTPUT="dist/${PACKAGE}_${VERSION}_${ARCH}.deb"

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "Error: dpkg-deb is required. Install it with: sudo apt install dpkg-dev" >&2
  exit 1
fi

rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/lib/revfur" "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" "$ROOT/usr/share/doc/revfur"

install -m 0644 main.py revfur_core.py furparser.py "$ROOT/usr/lib/revfur/"
install -m 0644 README.md CHANGELOG.md LICENSE "$ROOT/usr/share/doc/revfur/"
install -m 0644 packaging/io.github.mildannerofc.revfur.desktop \
  "$ROOT/usr/share/applications/io.github.mildannerofc.revfur.desktop"
# Normalise modes because some shared/WSL workspaces create setgid directories.
find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT" -type d -exec chmod g-s {} +

cat > "$ROOT/usr/bin/revfur" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/revfur/main.py "$@"
EOF
chmod 0755 "$ROOT/usr/bin/revfur"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: revfur
Version: ${VERSION}
Section: sound
Priority: optional
Architecture: ${ARCH}
Maintainer: RevFUR contributors
Depends: python3 (>= 3.10), python3-tk
Homepage: https://github.com/mildannerofc
Description: Furnace Tracker sequence disassembler and editor
 RevFUR reverses the complete Furnace sequence across all channels, and can
 extract or apply sequence-only edits without rewriting instrument, sample,
 or wavetable asset blocks.
EOF

mkdir -p dist
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}" dpkg-deb --root-owner-group --build "$ROOT" "$OUTPUT"
echo "Debian package created: $OUTPUT"
