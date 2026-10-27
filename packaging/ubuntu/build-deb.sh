#!/usr/bin/env bash
# Build build/filebridge_<version>_all.deb  (needs dpkg-deb, present on Ubuntu/Debian)
set -euo pipefail
cd "$(dirname "$0")/../.."
VER=$(python3 -c "import filebridge;print(filebridge.VERSION)")
PKG=build/deb
rm -rf "$PKG" && mkdir -p "$PKG/DEBIAN" "$PKG/usr/lib/filebridge" "$PKG/usr/bin" \
  "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/512x512/apps" "$PKG/usr/share/pixmaps" \
  "$PKG/usr/share/doc/filebridge"
install -m 644 filebridge.py "$PKG/usr/lib/filebridge/filebridge.py"
install -m 755 packaging/ubuntu/filebridge "$PKG/usr/bin/filebridge"
install -m 644 packaging/ubuntu/filebridge.desktop "$PKG/usr/share/applications/filebridge.desktop"
install -m 644 assets/icon.png "$PKG/usr/share/icons/hicolor/512x512/apps/filebridge.png"
install -m 644 assets/icon.png "$PKG/usr/share/pixmaps/filebridge.png"
install -m 644 README.md "$PKG/usr/share/doc/filebridge/README.md"
SIZE=$(du -sk "$PKG/usr" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CTL
Package: filebridge
Version: $VER
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.8)
Installed-Size: $SIZE
Maintainer: Sree Ram M S <sreerammsofficial@gmail.com>
Homepage: https://github.com/Sree-Ram-MS/File-Bridge
Description: Share folders on your local network with permission control
 FileBridge shares a folder over the LAN. Other devices open the shown
 address in a browser, request access, and the owner approves each device
 with view, upload or full permissions. Includes a file explorer UI.
CTL
dpkg-deb --build --root-owner-group "$PKG" "build/filebridge_${VER}_all.deb"
echo "Built build/filebridge_${VER}_all.deb"
