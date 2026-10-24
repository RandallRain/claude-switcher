#!/bin/bash
# 在指定输出位置构建 App;不会安装服务、读取账号或修改系统设置。
set -euo pipefail
SOURCE_ROOT="${1:?需要源码根目录}"
OUTPUT="${2:?需要输出 App 路径}"
EDITION="${3:-fork}"
case "$EDITION" in
    fork) NAME="Claude切换器-fork"; EXECUTABLE="ClaudeSwitcher-fork"; IDENTIFIER="local.claude-switcher-fork" ;;
    upstream) NAME="Claude切换器"; EXECUTABLE="ClaudeSwitcher"; IDENTIFIER="local.claude-switcher" ;;
    *) echo "未知版本: $EDITION" >&2; exit 2 ;;
esac
[ ! -e "$OUTPUT" ] || { echo "构建输出已存在: $OUTPUT" >&2; exit 1; }
mkdir -p "$OUTPUT/Contents/MacOS" "$OUTPUT/Contents/Resources"
swiftc -O -parse-as-library -target "$(uname -m)-apple-macos13.0" \
    -module-cache-path "${TMPDIR:-/tmp}/claude-switcher-${EDITION}-module-cache" \
    "$SOURCE_ROOT/app/main.swift" -o "$OUTPUT/Contents/MacOS/$EXECUTABLE" \
    -framework SwiftUI -framework AppKit
if [ -f "$SOURCE_ROOT/assets/icon-2048.png" ]; then
    ICON_TEMP=$(mktemp -d)
    trap 'rm -rf "$ICON_TEMP"' EXIT
    ICONSET="$ICON_TEMP/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 128 256 512; do
        sips -z "$sz" "$sz" "$SOURCE_ROOT/assets/icon-2048.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z $((sz*2)) $((sz*2)) "$SOURCE_ROOT/assets/icon-2048.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$OUTPUT/Contents/Resources/AppIcon.icns"
fi
cat > "$OUTPUT/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
    <key>CFBundleIdentifier</key><string>$IDENTIFIER</string>
    <key>CFBundleName</key><string>$NAME</string>
    <key>CFBundleDisplayName</key><string>$NAME</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
codesign --force --deep -s - "$OUTPUT"
