from __future__ import annotations

import argparse
import hashlib
import stat
import zipfile
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a deterministic Continuum Windows runtime archive."
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("archive", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    bundle = args.bundle.resolve()
    archive = args.archive.resolve()
    if not bundle.is_dir():
        raise SystemExit(f"bundle directory does not exist: {bundle}")
    if archive.exists():
        raise SystemExit(f"archive already exists: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)

    paths = [bundle, *sorted(bundle.rglob("*"))]
    with zipfile.ZipFile(
        archive,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        strict_timestamps=True,
    ) as output:
        for path in paths:
            relative = path.relative_to(bundle.parent).as_posix()
            is_directory = path.is_dir()
            name = f"{relative}/" if is_directory else relative
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (
                (stat.S_IFDIR | 0o755) if is_directory else (stat.S_IFREG | 0o644)
            ) << 16
            if is_directory:
                output.writestr(info, b"")
            elif path.is_file():
                output.writestr(info, path.read_bytes())
            else:
                raise SystemExit(f"unsupported bundle member type: {relative}")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = archive.with_name(f"{archive.name}.sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
