#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LINUX_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VERSION=${VERSION:-$(python3 -c 'from mwb_linux import __version__; print(__version__)')}
ARCH=${ARCH:-$(dpkg --print-architecture)}
BUILD_ROOT="$LINUX_DIR/build/deb-root"
DIST_DIR="$LINUX_DIR/dist"
PACKAGE_FILE="$DIST_DIR/powertoys-mouse-without-borders_${VERSION}_${ARCH}.deb"

case "$ARCH" in
    amd64|arm64) ;;
    *)
        echo "Unsupported Debian architecture: $ARCH (expected amd64 or arm64)." >&2
        exit 1
        ;;
esac

cargo build --manifest-path "$LINUX_DIR/portal-bridge/Cargo.toml" --locked --release
python3 -m compileall -q "$LINUX_DIR/mwb_linux"

rm -rf "$BUILD_ROOT"
mkdir -p \
    "$BUILD_ROOT/DEBIAN" \
    "$BUILD_ROOT/usr/bin" \
    "$BUILD_ROOT/usr/lib/powertoys-mouse-without-borders" \
    "$BUILD_ROOT/usr/lib/systemd/user" \
    "$BUILD_ROOT/usr/share/applications" \
    "$BUILD_ROOT/usr/share/metainfo" \
    "$BUILD_ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$BUILD_ROOT/usr/share/doc/powertoys-mouse-without-borders" \
    "$DIST_DIR"

sed -e "s/@VERSION@/$VERSION/g" -e "s/@ARCH@/$ARCH/g" \
    "$SCRIPT_DIR/control" > "$BUILD_ROOT/DEBIAN/control"
install -m 0755 "$SCRIPT_DIR/postinst" "$BUILD_ROOT/DEBIAN/postinst"
cp -a "$LINUX_DIR/mwb_linux" "$BUILD_ROOT/usr/lib/powertoys-mouse-without-borders/"
find "$BUILD_ROOT/usr/lib/powertoys-mouse-without-borders" \
    -type d -name __pycache__ -prune -exec rm -rf '{}' ';'
install -m 0755 "$LINUX_DIR/portal-bridge/target/release/mwb-portal-bridge" \
    "$BUILD_ROOT/usr/lib/powertoys-mouse-without-borders/mwb-portal-bridge"
install -m 0755 "$LINUX_DIR/resources/powertoys-mouse-without-borders" \
    "$BUILD_ROOT/usr/bin/powertoys-mouse-without-borders"
install -m 0644 "$LINUX_DIR/resources/powertoys-mwb-linux.service" \
    "$BUILD_ROOT/usr/lib/systemd/user/powertoys-mwb-linux.service"
install -m 0644 \
    "$LINUX_DIR/resources/app-io.github.NaveDanan.MouseWithoutBorders.service" \
    "$BUILD_ROOT/usr/lib/systemd/user/app-io.github.NaveDanan.MouseWithoutBorders.service"
install -m 0644 "$LINUX_DIR/resources/io.github.NaveDanan.MouseWithoutBorders.desktop" \
    "$BUILD_ROOT/usr/share/applications/io.github.NaveDanan.MouseWithoutBorders.desktop"
install -m 0644 "$LINUX_DIR/resources/io.github.NaveDanan.MouseWithoutBorders.metainfo.xml" \
    "$BUILD_ROOT/usr/share/metainfo/io.github.NaveDanan.MouseWithoutBorders.metainfo.xml"
install -m 0644 "$LINUX_DIR/mwb_linux/icons/hicolor/scalable/apps/io.github.NaveDanan.MouseWithoutBorders.svg" \
    "$BUILD_ROOT/usr/share/icons/hicolor/scalable/apps/io.github.NaveDanan.MouseWithoutBorders.svg"
install -m 0644 "$LINUX_DIR/README.md" \
    "$BUILD_ROOT/usr/share/doc/powertoys-mouse-without-borders/README.md"
python3 "$SCRIPT_DIR/generate-third-party-notices.py" \
    "$LINUX_DIR/portal-bridge/Cargo.toml" \
    > "$BUILD_ROOT/usr/share/doc/powertoys-mouse-without-borders/THIRD-PARTY-NOTICES.md"
install -m 0644 "$LINUX_DIR/LICENSE" \
    "$BUILD_ROOT/usr/share/doc/powertoys-mouse-without-borders/copyright"

find "$BUILD_ROOT" -type d -exec chmod 0755 '{}' ';'
find "$BUILD_ROOT" -type f -exec chmod 0644 '{}' ';'
chmod 0755 \
    "$BUILD_ROOT/DEBIAN/postinst" \
    "$BUILD_ROOT/usr/bin/powertoys-mouse-without-borders" \
    "$BUILD_ROOT/usr/lib/powertoys-mouse-without-borders/mwb-portal-bridge"
dpkg-deb --root-owner-group --build "$BUILD_ROOT" "$PACKAGE_FILE"
echo "$PACKAGE_FILE"
