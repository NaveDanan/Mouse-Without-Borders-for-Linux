#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LINUX_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VERSION=${VERSION:-$(python3 -c 'from mwb_linux import __version__; print(__version__)')}
BUILD_ROOT="$LINUX_DIR/build/appimage"
APPDIR="$BUILD_ROOT/MouseWithoutBorders.AppDir"
DIST_DIR="$LINUX_DIR/dist"
APP_ID=io.github.NaveDanan.MouseWithoutBorders

case "${APPIMAGE_ARCH:-$(uname -m)}" in
    x86_64|amd64) APPIMAGE_ARCH=x86_64 ;;
    aarch64|arm64) APPIMAGE_ARCH=aarch64 ;;
    *)
        echo "Unsupported AppImage architecture: ${APPIMAGE_ARCH:-$(uname -m)}" >&2
        exit 1
        ;;
esac

python3 -c 'import PyInstaller' 2>/dev/null || {
    echo "PyInstaller is required to build the AppImage." >&2
    exit 1
}
if [ -z "${APPIMAGETOOL:-}" ] || [ ! -x "$APPIMAGETOOL" ]; then
    echo "Set APPIMAGETOOL to an executable appimagetool AppImage." >&2
    exit 1
fi

cargo build --manifest-path "$LINUX_DIR/portal-bridge/Cargo.toml" --locked --release
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT" "$DIST_DIR"

python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --name powertoys-mouse-without-borders \
    --distpath "$BUILD_ROOT/pyinstaller-dist" \
    --workpath "$BUILD_ROOT/pyinstaller-work" \
    --specpath "$BUILD_ROOT" \
    --paths "$LINUX_DIR" \
    --add-data "$LINUX_DIR/mwb_linux/style.css:mwb_linux" \
    --add-data "$LINUX_DIR/mwb_linux/hero.svg:mwb_linux" \
    --add-data "$LINUX_DIR/mwb_linux/icons:mwb_linux/icons" \
    --hidden-import gi.repository.Adw \
    --hidden-import gi.repository.Gdk \
    --hidden-import gi.repository.GdkX11 \
    --hidden-import gi.repository.GdkPixbuf \
    --hidden-import gi.repository.Gio \
    --hidden-import gi.repository.GLib \
    --hidden-import gi.repository.Gtk \
    "$SCRIPT_DIR/appimage-entry.py"

mkdir -p \
    "$APPDIR/usr/lib/powertoys-mouse-without-borders" \
    "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/doc/powertoys-mouse-without-borders" \
    "$APPDIR/usr/share/metainfo" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a "$BUILD_ROOT/pyinstaller-dist/powertoys-mouse-without-borders/." \
    "$APPDIR/usr/lib/powertoys-mouse-without-borders/"
install -m 0755 "$LINUX_DIR/portal-bridge/target/release/mwb-portal-bridge" \
    "$APPDIR/usr/lib/powertoys-mouse-without-borders/mwb-portal-bridge"
install -m 0755 "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
install -m 0644 "$LINUX_DIR/resources/$APP_ID.desktop" \
    "$APPDIR/$APP_ID.desktop"
install -m 0644 "$LINUX_DIR/resources/$APP_ID.desktop" \
    "$APPDIR/usr/share/applications/$APP_ID.desktop"
install -m 0644 "$LINUX_DIR/resources/$APP_ID.metainfo.xml" \
    "$APPDIR/usr/share/metainfo/$APP_ID.metainfo.xml"
install -m 0644 \
    "$LINUX_DIR/mwb_linux/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$APPDIR/$APP_ID.svg"
install -m 0644 \
    "$LINUX_DIR/mwb_linux/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -m 0644 "$LINUX_DIR/LICENSE" \
    "$APPDIR/usr/share/doc/powertoys-mouse-without-borders/LICENSE"
install -m 0644 "$LINUX_DIR/README.md" \
    "$APPDIR/usr/share/doc/powertoys-mouse-without-borders/README.md"
python3 "$SCRIPT_DIR/generate-third-party-notices.py" \
    "$LINUX_DIR/portal-bridge/Cargo.toml" \
    > "$APPDIR/usr/share/doc/powertoys-mouse-without-borders/THIRD-PARTY-NOTICES.md"

OUTPUT="$DIST_DIR/Mouse-Without-Borders-$VERSION-$APPIMAGE_ARCH.AppImage"
ARCH="$APPIMAGE_ARCH" VERSION="$VERSION" \
    "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT"
chmod 0755 "$OUTPUT"
echo "$OUTPUT"
