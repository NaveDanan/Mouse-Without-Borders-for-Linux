#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LINUX_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VERSION=${VERSION:-$(python3 -c 'from mwb_linux import __version__; print(__version__)')}
BUILD_ROOT=$(mktemp -d)
DIST_DIR="$LINUX_DIR/dist"
SOURCE_NAME="powertoys-mouse-without-borders-$VERSION"

cleanup() {
    rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT HUP INT TERM

command -v rpmbuild >/dev/null 2>&1 || {
    echo "rpmbuild is required to build the RPM package." >&2
    exit 1
}

mkdir -p \
    "$BUILD_ROOT/rpmbuild/BUILD" \
    "$BUILD_ROOT/rpmbuild/BUILDROOT" \
    "$BUILD_ROOT/rpmbuild/RPMS" \
    "$BUILD_ROOT/rpmbuild/SOURCES" \
    "$BUILD_ROOT/rpmbuild/SPECS" \
    "$BUILD_ROOT/rpmbuild/SRPMS" \
    "$DIST_DIR"

tar \
    --exclude=.git \
    --exclude=build \
    --exclude=dist \
    --exclude=portal-bridge/target \
    --exclude='**/__pycache__' \
    --transform "s|^\./|$SOURCE_NAME/|" \
    -czf "$BUILD_ROOT/rpmbuild/SOURCES/$SOURCE_NAME.tar.gz" \
    -C "$LINUX_DIR" .
sed "s/@VERSION@/$VERSION/g" \
    "$SCRIPT_DIR/powertoys-mouse-without-borders.spec" \
    > "$BUILD_ROOT/rpmbuild/SPECS/powertoys-mouse-without-borders.spec"

rpmbuild \
    --nodeps \
    --define "_topdir $BUILD_ROOT/rpmbuild" \
    -bb "$BUILD_ROOT/rpmbuild/SPECS/powertoys-mouse-without-borders.spec"
find "$BUILD_ROOT/rpmbuild/RPMS" -type f -name '*.rpm' -exec cp '{}' "$DIST_DIR/" ';'
find "$DIST_DIR" -maxdepth 1 -type f -name "powertoys-mouse-without-borders-$VERSION-*.rpm" -print
