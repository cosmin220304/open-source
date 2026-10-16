#!/bin/sh
# Builds ~/Applications/Gauntlet.app pointing at this checkout's watch.py.
set -e
here=$(cd "$(dirname "$0")" && pwd)
app="${1:-$HOME/Applications/Gauntlet.app}"
rm -rf "$app" && mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
swiftc -O "$here/Gauntlet.swift" -o "$app/Contents/MacOS/Gauntlet"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Gauntlet</string>
<key>CFBundleIdentifier</key><string>inc.light.gauntlet</string>
<key>CFBundleExecutable</key><string>Gauntlet</string>
<key>CFBundleIconFile</key><string>icon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
<key>GauntletWatch</key><string>$here/../skills/run/watch.py</string>
</dict></plist>
PLIST
# Icon drawn with AppKit (same mark as the page favicon), no image assets.
tmp=$(mktemp -d)
cat > "$tmp/icon.swift" <<'SWIFT'
import Cocoa
let s = 1024.0, img = NSImage(size: NSSize(width: s, height: s))
img.lockFocus()
NSColor(red: 0.09, green: 0.11, blue: 0.13, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 80, y: 80, width: 864, height: 864), xRadius: 190, yRadius: 190).fill()
let green = NSColor(red: 0.25, green: 0.73, blue: 0.31, alpha: 1)
let ring = NSBezierPath(ovalIn: NSRect(x: 216, y: 216, width: 592, height: 592)); ring.lineWidth = 48
green.withAlphaComponent(0.35).setStroke(); ring.stroke()
green.setFill(); NSBezierPath(ovalIn: NSRect(x: 352, y: 352, width: 320, height: 320)).fill()
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
SWIFT
swift "$tmp/icon.swift" "$tmp/icon.png"
mkdir "$tmp/icon.iconset"
for n in 16 32 128 256 512; do
  sips -z $n $n "$tmp/icon.png" --out "$tmp/icon.iconset/icon_${n}x${n}.png" >/dev/null
  sips -z $((n*2)) $((n*2)) "$tmp/icon.png" --out "$tmp/icon.iconset/icon_${n}x${n}@2x.png" >/dev/null
done
iconutil -c icns "$tmp/icon.iconset" -o "$app/Contents/Resources/icon.icns"
rm -rf "$tmp"
touch "$app"
echo "built $app"
