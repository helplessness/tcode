#!/bin/sh
set -eu

OUTPUT_ROOT="${TMPDIR:-/tmp}/tcode-computer-fixture"
APP="$OUTPUT_ROOT/TCode Computer Fixture.app"
CONTENTS="$APP/Contents"

mkdir -p "$CONTENTS/MacOS"
swiftc "$(dirname "$0")/ComputerUseFixture.swift" -o "$CONTENTS/MacOS/TCodeComputerFixture" -framework AppKit
cp "$(dirname "$0")/Info.plist" "$CONTENTS/Info.plist"
printf '%s\n' "$APP"
