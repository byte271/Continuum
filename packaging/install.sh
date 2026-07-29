#!/bin/sh
set -eu

usage() {
    echo "usage: install.sh --archive <HTTPS-URL-or-path> --sha256 <hex> [--prefix <absolute-path>]" >&2
}

archive_source=
expected_sha256=
install_prefix=${HOME:+$HOME/.local}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --archive)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            archive_source=$2
            shift 2
            ;;
        --sha256)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            expected_sha256=$2
            shift 2
            ;;
        --prefix)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            install_prefix=$2
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[ -n "$archive_source" ] || { usage; exit 2; }
[ -n "$expected_sha256" ] || { usage; exit 2; }
case "$expected_sha256" in
    *[!0-9a-f]*)
        echo "SHA-256 must be exactly 64 lowercase hexadecimal characters" >&2
        exit 2
        ;;
esac
if [ "${#expected_sha256}" -ne 64 ]; then
    echo "SHA-256 must be exactly 64 lowercase hexadecimal characters" >&2
    exit 2
fi
case "$install_prefix" in
    /*) ;;
    *)
        echo "install prefix must be an absolute path" >&2
        exit 2
        ;;
esac

system=$(uname -s)
machine=$(uname -m)
case "$system:$machine" in
    Linux:x86_64) target=linux-x86_64 ;;
    Darwin:arm64) target=macos-arm64 ;;
    *)
        echo "no self-contained Continuum bundle for $system $machine" >&2
        exit 1
        ;;
esac

temporary_root=${TMPDIR:-/tmp}
if [ ! -d "$temporary_root" ]; then
    temporary_root=$PWD
fi
temporary=$(mktemp -d "$temporary_root/continuum-install-XXXXXX")
cleanup() {
    rm -rf "$temporary"
}
trap cleanup EXIT HUP INT TERM
archive="$temporary/continuum.tar.gz"

case "$archive_source" in
    https://*)
        curl \
            --fail \
            --location \
            --proto '=https' \
            --show-error \
            --silent \
            --tlsv1.2 \
            --output "$archive" \
            "$archive_source"
        ;;
    http://*)
        echo "installer refuses non-HTTPS downloads" >&2
        exit 2
        ;;
    *)
        cp "$archive_source" "$archive"
        ;;
esac

if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256=$(sha256sum "$archive" | awk '{print $1}')
else
    actual_sha256=$(shasum -a 256 "$archive" | awk '{print $1}')
fi
if [ "$actual_sha256" != "$expected_sha256" ]; then
    echo "archive SHA-256 mismatch" >&2
    exit 1
fi

bundle_name="continuum-$target"
tar -tzf "$archive" > "$temporary/members.txt"
while IFS= read -r member; do
    case "$member" in
        "$bundle_name" | "$bundle_name"/*) ;;
        *)
            echo "unsafe or unexpected archive member: $member" >&2
            exit 1
            ;;
    esac
    case "/$member/" in
        */../*)
            echo "path traversal in archive member: $member" >&2
            exit 1
            ;;
    esac
done < "$temporary/members.txt"

tar -xzf "$archive" -C "$temporary"
staged="$temporary/$bundle_name"
"$staged/bin/continuum" doctor >/dev/null

library_dir="$install_prefix/lib/$bundle_name"
command_path="$install_prefix/bin/continuum"
if [ -e "$library_dir" ] || [ -L "$library_dir" ]; then
    echo "installation already exists: $library_dir" >&2
    exit 1
fi
if [ -e "$command_path" ] || [ -L "$command_path" ]; then
    echo "command path already exists: $command_path" >&2
    exit 1
fi

mkdir -p "$install_prefix/lib" "$install_prefix/bin"
mv "$staged" "$library_dir"
ln -s "../lib/$bundle_name/bin/continuum" "$command_path"
if ! "$command_path" doctor >/dev/null; then
    unlink "$command_path"
    mv "$library_dir" "$staged"
    echo "installed launcher failed its compatibility check; rolled back" >&2
    exit 1
fi

echo "Installed Continuum: $command_path"
echo "Verify: $command_path doctor"
echo "Uninstall: rm '$command_path' && rm -rf '$library_dir'"
