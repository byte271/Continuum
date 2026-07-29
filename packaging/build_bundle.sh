#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 2 ]]; then
    echo "usage: build_bundle.sh <linux-x86_64|macos-arm64> <absolute-output-parent>" >&2
    exit 2
fi

target="$1"
output_parent="$2"
case "$target" in
    linux-x86_64)
        builder_target="linux"
        expected_system="Linux"
        expected_architecture="x86_64"
        ;;
    macos-arm64)
        builder_target="macos"
        expected_system="Darwin"
        expected_architecture="arm64"
        ;;
    *)
        echo "unsupported bundle target: $target" >&2
        exit 2
        ;;
esac

if [[ "$output_parent" != /* ]]; then
    echo "output parent must be an absolute path" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
bundle_name="continuum-$target"
destination="$output_parent/$bundle_name"
archive="$output_parent/$bundle_name.tar.gz"
build_evidence="$output_parent/$bundle_name-build-evidence"
if [[
    -e "$destination"
    || -e "$archive"
    || -e "$archive.sha256"
    || -e "$build_evidence"
]]; then
    echo "bundle output already exists under $output_parent" >&2
    exit 2
fi

mkdir -p "$output_parent"
temporary_root="${RUNNER_TEMP:-${TMPDIR:-$output_parent}}"
if [[ ! -d "$temporary_root" ]]; then
    temporary_root="$output_parent"
fi
work_dir="$(mktemp -d "$temporary_root/continuum-bundle-XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
python_prefix="$work_dir/python-prefix"
bundle="$work_dir/$bundle_name"

RUNNER_TEMP="$temporary_root" \
TMPDIR="$temporary_root" \
"$repo_root/validation/cross_platform/build_cpython.sh" \
    "$builder_target" \
    "$python_prefix" \
    "$build_evidence"

mkdir -p "$bundle/bin" "$bundle/app" "$bundle/examples"
mv "$python_prefix" "$bundle/runtime"
cp -R "$repo_root/continuum" "$bundle/app/continuum"
find "$bundle/app" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$bundle/app" -type f -name '*.pyc' -delete
cp "$script_dir/continuum" "$bundle/bin/continuum"
chmod 0755 "$bundle/bin/continuum"
cp "$repo_root/examples/demo.py" "$bundle/examples/demo.py"
cp "$repo_root/examples/demo_input.txt" "$bundle/examples/demo_input.txt"
cp "$repo_root/LICENSE" "$bundle/LICENSE"
cp "$repo_root/README.md" "$bundle/README.md"
cp -R "$build_evidence" "$bundle/python-build-evidence"
rm -rf "$build_evidence"

git_commit="$(git -C "$repo_root" rev-parse HEAD)"
source_sha256="$(
    awk '{print $1}' "$repo_root/validation/cross_platform/cpython-3.12.13.sha256"
)"
PYTHONHOME="$bundle/runtime" \
PYTHONPATH="$bundle/app" \
"$bundle/runtime/bin/python3.12" - \
    "$bundle/runtime-manifest.json" \
    "$target" \
    "$expected_system" \
    "$expected_architecture" \
    "$git_commit" \
    "$source_sha256" <<'PY'
import json
import platform
import sys
from pathlib import Path

from continuum import IR_VERSION, __version__

(
    manifest_path,
    target,
    expected_system,
    expected_architecture,
    git_commit,
    source_sha256,
) = sys.argv[1:]
if platform.system() != expected_system:
    raise SystemExit(
        f"bundle system mismatch: {platform.system()} != {expected_system}"
    )
if platform.machine() != expected_architecture:
    raise SystemExit(
        f"bundle architecture mismatch: "
        f"{platform.machine()} != {expected_architecture}"
    )
if platform.python_version() != "3.12.13":
    raise SystemExit("bundle does not contain exact CPython 3.12.13")
manifest = {
    "architecture": expected_architecture,
    "bundle_target": target,
    "continuum_version": __version__,
    "cpython_source_sha256": source_sha256,
    "git_commit": git_commit,
    "ir_version": IR_VERSION,
    "python_implementation": platform.python_implementation(),
    "python_version": platform.python_version(),
    "self_contained": True,
    "system": expected_system,
}
Path(manifest_path).write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

"$bundle/bin/continuum" --version
"$bundle/bin/continuum" doctor --json > "$bundle/doctor-build-check.json"

self_test="$work_dir/bundle-self-test.py"
printf '%s\n' 'print("CONTINUUM_BUNDLE_OK")' > "$self_test"
self_test_output="$("$bundle/bin/continuum" run "$self_test")"
if [[ "$self_test_output" != "CONTINUUM_BUNDLE_OK" ]]; then
    echo "bundle self-test failed" >&2
    exit 1
fi

mv "$bundle" "$destination"
python3 "$script_dir/archive_bundle.py" "$destination" "$archive"
echo "Bundle: $destination"
echo "Archive: $archive"
echo "SHA-256: $archive.sha256"
