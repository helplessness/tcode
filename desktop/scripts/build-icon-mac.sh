#!/bin/bash
set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ASSET_DIR="$DESKTOP_DIR/assets"
ICONSET="$ASSET_DIR/icon.iconset"
SOURCE="$ASSET_DIR/icon.svg"
MASTER="$ASSET_DIR/icon-1024.png"

rm -rf "$ICONSET"
mkdir -p "$ICONSET"
sips -s format png "$SOURCE" --out "$MASTER" >/dev/null

for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$MASTER" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$MASTER" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done

iconutil -c icns "$ICONSET" -o "$ASSET_DIR/icon.icns"
rm -rf "$ICONSET" "$MASTER"
echo "$ASSET_DIR/icon.icns"
