#!/bin/sh
# Install or upgrade cpool (macOS only). Needs no Python or other tooling.
#
#   curl -fsSL https://raw.githubusercontent.com/ayorcodes/cpool/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/ayorcodes/cpool/main/install.sh | sh -s -- --uninstall
#
# Downloads a prebuilt binary into <home>/cpool.dist and links
# ~/.local/bin/cpool to it. The link is a stable path, which cpool's launchd
# plists rely on. Re-run to upgrade.
#
# Env: CPOOL_SRC (local tarball path or URL, replaces the download),
# CPOOL_BASE_URL (release download base), CPOOL_HOME_DIR (default
# ~/.local/share/cpool).

set -eu

MARKER="# added by cpool installer"

die() {
    echo "" >&2
    echo "error: $*" >&2
    exit 1
}

fetch() {
    case "$1" in
        http://* | https://*) curl -fsSL "$1" -o "$2" ;;
        *) cp "$1" "$2" ;;
    esac
}

add_to_path() {
    case "$(basename "${SHELL:-}")" in
        zsh) rc="$HOME/.zshrc" ;;
        bash)
            rc="$HOME/.bash_profile"
            [ ! -f "$rc" ] && [ -f "$HOME/.bashrc" ] && rc="$HOME/.bashrc"
            ;;
        *) rc="" ;;
    esac
    # shellcheck disable=SC2016 # literal $HOME/$PATH belong in the rc file
    line='export PATH="$HOME/.local/bin:$PATH"'
    if [ -n "$rc" ]; then
        if [ -f "$rc" ] && { grep -qF "$MARKER" "$rc" || grep -qF "$line" "$rc"; }; then
            :
        else
            # A missing trailing newline would glue our block onto the last line.
            [ -s "$rc" ] && [ -n "$(tail -c 1 "$rc")" ] && echo "" >> "$rc"
            printf '%s\n%s\n' "$MARKER" "$line" >> "$rc"
            echo "Added ~/.local/bin to PATH in $rc."
        fi
    fi
    echo "Open a new shell, or run: $line"
}

main() {
    [ "$(uname -s)" = "Darwin" ] || die "cpool only works on macOS."

    HOME_DIR="${CPOOL_HOME_DIR:-$HOME/.local/share/cpool}"
    DIST="$HOME_DIR/cpool.dist"
    BIN_DIR="$HOME/.local/bin"
    LINK="$BIN_DIR/cpool"

    if [ "${1:-}" = "--uninstall" ]; then
        if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
            die "$LINK is not a symlink; leaving it."
        fi
        rm -f "$LINK"
        rm -rf "$DIST" "$HOME_DIR/venv"
        echo "Removed $LINK and $DIST."
        echo "Left in place: ~/.cpool state and any PATH block in your shell rc file."
        return 0
    fi

    if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
        die "$LINK exists and is not a symlink; move it aside and re-run."
    fi

    case "$(uname -m)" in
        arm64) ARCH=arm64 ;;
        x86_64) ARCH=x64 ;;
        *) die "unsupported CPU: $(uname -m)." ;;
    esac

    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    NAME="cpool-darwin-$ARCH.tar.gz"
    SRC="${CPOOL_SRC:-${CPOOL_BASE_URL:-https://github.com/ayorcodes/cpool/releases/latest/download}/$NAME}"

    echo "Downloading cpool from $SRC..."
    fetch "$SRC" "$TMP/$NAME" || die "no build for this Mac yet ($ARCH): could not fetch $SRC."
    if fetch "$SRC.sha256" "$TMP/$NAME.sha256" 2>/dev/null; then
        # The sidecar names the tarball as published; check ours under that name.
        (cd "$TMP" && printf '%s  %s\n' "$(awk '{print $1}' "$NAME.sha256")" "$NAME" | shasum -a 256 -c - >/dev/null) ||
            die "checksum mismatch for $SRC."
    elif [ -z "${CPOOL_SRC:-}" ]; then
        die "could not fetch $SRC.sha256."
    fi

    mkdir -p "$HOME_DIR" "$TMP/x"
    tar -xzf "$TMP/$NAME" -C "$TMP/x" || die "could not extract $NAME."
    [ -x "$TMP/x/cpool.dist/cpool-bin" ] || die "$NAME does not contain cpool.dist/cpool-bin."
    xattr -dr com.apple.quarantine "$TMP/x/cpool.dist" 2>/dev/null || true

    rm -rf "$DIST.new" "$HOME_DIR/venv"
    mv "$TMP/x/cpool.dist" "$DIST.new"
    rm -rf "$DIST"
    mv "$DIST.new" "$DIST"

    mkdir -p "$BIN_DIR"
    rm -f "$LINK"
    ln -s "$DIST/cpool-bin" "$LINK"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) add_to_path ;;
    esac

    "$LINK" --help >/dev/null || die "installed cpool failed its smoke test ($LINK --help)."

    echo ""
    echo "Installed: $LINK"
    echo "Next: cpool adopt ~/.claude --name main"
}

main "$@"
