#!/bin/bash
# Builds "Fiber Voice.app" and a drag-to-install DMG in macapp/dist/.
#
# Requirements on the build Mac: Xcode Command Line Tools (swiftc), Python 3.11+,
# and Homebrew codec libraries:  brew install opencore-amr vo-amrwbenc
#
# Optional: SIGN_IDENTITY="Developer ID Application: …" signs with hardened runtime;
#           NOTARY_PROFILE=<notarytool keychain profile> also notarizes the DMG.
# The result runs on Macs with the same CPU architecture as the build Mac.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
GATEWAY="$ROOT/gateway"
BUILD="$HERE/build"
DIST="$HERE/dist"
NAME="Fiber Voice"
APP="$DIST/$NAME.app"
VERSION="${VERSION:-1.0.0}"
ARCH="$(uname -m)"
MIN_MACOS=13.0
PYTHON="${PYTHON:-python3}"
SIGN_IDENTITY="${SIGN_IDENTITY:--}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

mkdir -p "$BUILD" "$DIST"

step "Python build environment"
if [ ! -x "$BUILD/venv/bin/python" ]; then
  "$PYTHON" -m venv "$BUILD/venv"
fi
"$BUILD/venv/bin/python" -m pip install --quiet --no-cache-dir --upgrade pip
"$BUILD/venv/bin/python" -m pip install --quiet --no-cache-dir -r "$GATEWAY/requirements.txt" pyinstaller

step "Gateway tests"
(cd "$GATEWAY" && "$BUILD/venv/bin/python" -m unittest discover -s tests 2>&1 | tail -3)

step "Freezing gateway with PyInstaller"
"$BUILD/venv/bin/pyinstaller" --noconfirm --clean --log-level WARN \
  --onedir --name fiber-gateway \
  --distpath "$BUILD/pyi-dist" --workpath "$BUILD/pyi-work" --specpath "$BUILD" \
  --paths "$GATEWAY" \
  --add-data "$GATEWAY/static:static" \
  --hidden-import cryptography.x509 \
  --collect-submodules aiortc --collect-submodules aioice \
  --osx-bundle-identifier in.fibervoice.gateway \
  "$GATEWAY/server.py"

step "Compiling app"
swiftc -O -target "$ARCH-apple-macos$MIN_MACOS" -o "$BUILD/FiberVoice" "$HERE"/Sources/*.swift \
  -framework AppKit -framework WebKit -framework UserNotifications -framework ServiceManagement -framework Contacts
swiftc -O -o "$BUILD/make-icon" "$HERE/Tools/MakeIcon.swift" -framework AppKit

step "Icon"
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
"$BUILD/make-icon" "$BUILD/icon-1024.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"

step "Assembling $NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/codecs"
cp "$BUILD/FiberVoice" "$APP/Contents/MacOS/FiberVoice"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/"
cp -R "$BUILD/pyi-dist/fiber-gateway" "$APP/Contents/Resources/gateway"

# AMR codecs: OpenCORE (AMR-NB, AMR-WB decode) and VisualOn (AMR-WB encode).
for lib in opencore-amr:opencore-amrnb opencore-amr:opencore-amrwb vo-amrwbenc:vo-amrwbenc; do
  formula="${lib%%:*}"; name="${lib##*:}"
  prefix="$(brew --prefix "$formula" 2>/dev/null || true)"
  src="$prefix/lib/lib$name.dylib"
  if [ ! -f "$src" ]; then
    echo "Missing $src — run: brew install opencore-amr vo-amrwbenc" >&2
    exit 1
  fi
  dest="$APP/Contents/Resources/codecs/lib$name.dylib"
  cp -L "$src" "$dest"
  chmod 644 "$dest"
  install_name_tool -id "@rpath/lib$name.dylib" "$dest" 2>/dev/null
done
cp "$(brew --prefix opencore-amr)/LICENSE" "$APP/Contents/Resources/codecs/LICENSE-opencore-amr" 2>/dev/null || true
cp "$(brew --prefix vo-amrwbenc)/COPYING" "$APP/Contents/Resources/codecs/LICENSE-vo-amrwbenc" 2>/dev/null || true

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleExecutable</key><string>FiberVoice</string>
  <key>CFBundleIdentifier</key><string>in.fibervoice.app</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.social-networking</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticTermination</key><false/>
  <key>NSSupportsSuddenTermination</key><false/>
  <key>NSContactsUsageDescription</key><string>$NAME shows names from your Contacts in your call log and on incoming calls. Contacts stay on this Mac.</string>
  <key>NSCameraUsageDescription</key><string>$NAME uses the camera for video calls on your landline.</string>
  <key>NSMicrophoneUsageDescription</key><string>$NAME uses the microphone for your landline calls.</string>
  <key>NSLocalNetworkUsageDescription</key><string>$NAME connects to your JioFiber router to place and receive landline calls.</string>
  <key>NSBonjourServices</key><array/>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key><string>Phone number</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>CFBundleURLSchemes</key><array><string>tel</string><string>callto</string></array>
    </dict>
  </array>
  <key>NSHumanReadableCopyright</key><string>Independent project. Not affiliated with Jio.</string>
</dict>
</plist>
PLIST

step "Signing ($([ "$SIGN_IDENTITY" = "-" ] && echo ad-hoc || echo "$SIGN_IDENTITY"))"
SIGN_ARGS=(--force --sign "$SIGN_IDENTITY" --timestamp=none)
if [ "$SIGN_IDENTITY" != "-" ]; then
  SIGN_ARGS=(--force --sign "$SIGN_IDENTITY" --timestamp --options runtime --entitlements "$HERE/entitlements.plist")
fi
# Sign nested code inside-out, then the app itself.
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q 'Mach-O'; then
    codesign "${SIGN_ARGS[@]}" "$f" 2>&1 | grep -v 'replacing existing signature' || true
  fi
done < <(find "$APP/Contents/Resources" -type f -print0)
codesign "${SIGN_ARGS[@]}" "$APP" 2>&1 | grep -v 'replacing existing signature' || true
codesign --verify --deep --strict "$APP"

step "Disk image"
STAGE="$BUILD/dmg"
DMG="$DIST/$NAME-$VERSION-$ARCH.dmg"
rm -rf "$STAGE" "$DMG" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$HERE/README.md" "$STAGE/Read Me.md"
# hdiutil occasionally fails transiently (e.g. while Spotlight indexes the staging folder); retry.
for attempt in 1 2 3; do
  if hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG" 2>&1 | grep -v deprecated; then :; fi
  [ -f "$DMG" ] && break
  echo "Retrying disk image ($attempt)…"; sleep 2
done
[ -f "$DMG" ] || { echo "Could not create $DMG" >&2; exit 1; }
rm -rf "$STAGE" "$BUILD/pyi-work" "$BUILD/pyi-dist" "$ICONSET" "$BUILD"/{AppIcon.icns,FiberVoice,icon-1024.png,make-icon,fiber-gateway.spec}  # keep only the venv between builds
if [ "$SIGN_IDENTITY" != "-" ]; then
  codesign --force --sign "$SIGN_IDENTITY" --timestamp "$DMG"
  if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
  fi
fi

step "Done"
du -sh "$APP" "$DMG"
echo "App: $APP"
echo "DMG: $DMG"
