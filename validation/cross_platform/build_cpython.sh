#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 3 ]]; then
    echo "usage: build_cpython.sh <linux|macos> <absolute-prefix> <evidence-dir>" >&2
    exit 2
fi

target_platform="$1"
install_prefix="$2"
evidence_dir="$3"

if [[ "$target_platform" != "linux" && "$target_platform" != "macos" ]]; then
    echo "target platform must be linux or macos" >&2
    exit 2
fi
if [[ "$install_prefix" != /* || "$evidence_dir" != /* ]]; then
    echo "install prefix and evidence directory must be absolute" >&2
    exit 2
fi
if [[ -e "$install_prefix" || -e "$evidence_dir" ]]; then
    echo "install prefix and evidence directory must not already exist" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
checksum_file="$script_dir/cpython-3.12.13.sha256"
source_name="Python-3.12.13.tar.xz"
source_url="https://www.python.org/ftp/python/3.12.13/$source_name"
expected_sha256="$(awk -v name="$source_name" '$2 == name {print $1}' "$checksum_file")"

if [[ ! "$expected_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "pinned CPython checksum is missing or invalid" >&2
    exit 1
fi

mkdir -p "$evidence_dir"
work_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/continuum-cpython-3.12.13-XXXXXX")"
source_archive="$work_dir/$source_name"
source_dir="$work_dir/Python-3.12.13"
build_log="$evidence_dir/build.log"
exec > >(tee "$build_log") 2>&1

echo "CPython source URL: $source_url"
echo "Expected SHA-256: $expected_sha256"
echo "Target platform: $target_platform"
echo "Install prefix: $install_prefix"

curl \
    --fail \
    --location \
    --proto '=https' \
    --show-error \
    --silent \
    --tlsv1.2 \
    --output "$source_archive" \
    "$source_url"

actual_sha256="$(shasum -a 256 "$source_archive" | awk '{print $1}')"
printf '%s  %s\n' "$actual_sha256" "$source_name" \
    > "$evidence_dir/source.sha256"
printf '%s\n' "$source_url" > "$evidence_dir/source-url.txt"
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    echo "CPython source SHA-256 mismatch" >&2
    exit 1
fi

tar -xf "$source_archive" -C "$work_dir"
mkdir -p "$install_prefix"

configure_args=(
    "./configure"
    "--prefix=$install_prefix"
    "--with-ensurepip=no"
)
printf '%q ' "${configure_args[@]}" > "$evidence_dir/configure-command.txt"
printf '\n' >> "$evidence_dir/configure-command.txt"

{
    echo '$ uname -a'
    uname -a
    echo
    echo '$ cc --version'
    cc --version || true
    echo
    echo '$ cc -v'
    cc -v || true
} > "$evidence_dir/compiler.txt" 2>&1

if [[ "$target_platform" == "linux" ]]; then
    build_jobs="$(getconf _NPROCESSORS_ONLN)"
else
    build_jobs="$(sysctl -n hw.logicalcpu)"
fi

cd "$source_dir"
"${configure_args[@]}"
make -j "$build_jobs"
make install

python_executable="$install_prefix/bin/python3.12"
if [[ ! -x "$python_executable" ]]; then
    echo "built Python executable is missing" >&2
    exit 1
fi

"$python_executable" --version > "$evidence_dir/python-version.txt" 2>&1
"$python_executable" -c \
    'import platform, sys; print(platform.system()); print(platform.machine()); print(sys.executable)' \
    > "$evidence_dir/python-platform.txt"
file "$python_executable" > "$evidence_dir/python-file.txt"
printf '%s\n' "$python_executable" > "$evidence_dir/python-executable.txt"

if [[ "$(<"$evidence_dir/python-version.txt")" != "Python 3.12.13" ]]; then
    echo "built interpreter is not exactly Python 3.12.13" >&2
    exit 1
fi

python_system="$(sed -n '1p' "$evidence_dir/python-platform.txt")"
python_machine="$(sed -n '2p' "$evidence_dir/python-platform.txt")"
if [[ "$target_platform" == "linux" ]]; then
    [[ "$python_system" == "Linux" ]]
    [[ "$python_machine" == "x86_64" ]]
    grep -Eiq 'ELF 64-bit.*x86-64' "$evidence_dir/python-file.txt"
else
    [[ "$python_system" == "Darwin" ]]
    [[ "$python_machine" == "arm64" ]]
    grep -Eiq 'Mach-O 64-bit.*arm64' "$evidence_dir/python-file.txt"
fi

"$python_executable" - "$target_platform" "$expected_sha256" "$source_url" \
    "$install_prefix" "$evidence_dir/configure-command.txt" \
    > "$evidence_dir/build-metadata.json" <<'PY'
import json
import platform
import sys
from pathlib import Path

target, source_sha256, source_url, prefix, configure_path = sys.argv[1:]
metadata = {
    "builder_target": target,
    "configure_command": Path(configure_path).read_text(encoding="utf-8").strip(),
    "install_prefix": prefix,
    "python_executable": sys.executable,
    "python_implementation": platform.python_implementation(),
    "python_machine": platform.machine(),
    "python_system": platform.system(),
    "python_version": platform.python_version(),
    "source_sha256": source_sha256,
    "source_tarball": "Python-3.12.13.tar.xz",
    "source_url": source_url,
}
print(json.dumps(metadata, indent=2, sort_keys=True))
PY

echo "Built interpreter: $python_executable"
echo "CPython 3.12.13 native build evidence complete."
