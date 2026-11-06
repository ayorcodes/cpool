#!/bin/sh
# Builds the Nuitka standalone binary and packages it for a GitHub release:
#   release/cpool-darwin-<arm64|x64>.tar.gz   (top-level dir cpool.dist/)
#   release/cpool-darwin-<arm64|x64>.tar.gz.sha256
# Runs locally on the maintainer's Mac. A failed build never touches release/.
set -eu

NUITKA_VERSION="4.2.1"
MIN_PYTHON_MINOR=12

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_TMP="$(mktemp -d)"
BUILD_VENV="$BUILD_TMP/venv"

cleanup() {
    rm -rf "$BUILD_TMP"
}
trap cleanup EXIT

find_python() {
    for candidate in python3.13 python3.12 python3.14 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_PYTHON_MINOR) else 1)"; then
                command -v "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python)" || {
    echo "error: build-binary.sh needs Python 3.${MIN_PYTHON_MINOR}+ on PATH." >&2
    exit 1
}

echo "Using $PYTHON"
# cpool/__main__.py uses a relative import, which fails as a Nuitka main module.
ENTRY="$BUILD_TMP/cpool_entry.py"
printf 'from cpool import cli\n\nraise SystemExit(cli.main())\n' > "$ENTRY"
"$PYTHON" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip
"$BUILD_VENV/bin/pip" install --quiet "nuitka==$NUITKA_VERSION" "$PKG_ROOT"

# Nuitka builds for the interpreter it runs under; the target arch must match.
case "$(uname -m)" in
    x86_64) TARGET_ARCH=x86_64; ARCH=x64 ;;
    *) TARGET_ARCH=arm64; ARCH=arm64 ;;
esac

# xcrun caches the resolved SDK path; an Xcode update that drops that SDK
# leaves otool failing on the stale path, which Nuitka treats as fatal.
xcrun --kill-cache 2>/dev/null || true

"$BUILD_VENV/bin/python" -m nuitka \
    --standalone \
    --assume-yes-for-downloads \
    --macos-target-arch="$TARGET_ARCH" \
    --python-flag=no_docstrings \
    --include-package=cpool \
    --include-package=anthropic \
    --include-package-data=cpool \
    --include-data-dir="$PKG_ROOT/plugin=cpool/plugin" \
    --output-filename=cpool-bin \
    --output-dir="$BUILD_TMP/out" \
    --remove-output \
    "$ENTRY"

DIST_OUT="$BUILD_TMP/out/cpool_entry.dist"
if [ ! -d "$DIST_OUT" ]; then
    echo "error: Nuitka did not produce $DIST_OUT" >&2
    exit 1
fi
BIN="$DIST_OUT/cpool-bin"

# Run from an empty dir so nothing resolves against the source tree.
SMOKE_DIR="$BUILD_TMP/smoke"
mkdir -p "$SMOKE_DIR"
(
    cd "$SMOKE_DIR"
    export HOME="$SMOKE_DIR"
    "$BIN" --help >/dev/null
    "$BIN" status >/dev/null
    "$BIN" advise --help >/dev/null
)
for f in cpool/plugin/hooks/hooks.json cpool/assets/ask-fable/SKILL.md; do
    [ -f "$DIST_OUT/$f" ] || { echo "error: dist is missing $f" >&2; exit 1; }
done

NAME="cpool-darwin-$ARCH"
STAGE="$BUILD_TMP/stage"
mkdir -p "$STAGE"
mv "$DIST_OUT" "$STAGE/cpool.dist"
tar -czf "$STAGE/$NAME.tar.gz" -C "$STAGE" cpool.dist
(cd "$STAGE" && shasum -a 256 "$NAME.tar.gz" > "$NAME.tar.gz.sha256")

mkdir -p "$PKG_ROOT/release"
mv "$STAGE/$NAME.tar.gz" "$PKG_ROOT/release/$NAME.tar.gz.new"
mv "$STAGE/$NAME.tar.gz.sha256" "$PKG_ROOT/release/$NAME.tar.gz.sha256"
mv "$PKG_ROOT/release/$NAME.tar.gz.new" "$PKG_ROOT/release/$NAME.tar.gz"

echo "Built release/$NAME.tar.gz"
