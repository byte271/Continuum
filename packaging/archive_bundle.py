from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import tarfile
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a deterministic Continuum runtime archive."
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
    with archive.open("xb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            mtime=0,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as output:
                for path in paths:
                    relative = path.relative_to(bundle.parent)
                    info = output.gettarinfo(str(path), arcname=relative.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if info.isfile():
                        with path.open("rb") as handle:
                            output.addfile(info, handle)
                    elif info.issym():
                        output.addfile(info)
                    elif info.isdir():
                        output.addfile(info)
                    else:
                        raise SystemExit(
                            f"unsupported bundle member type: {relative}"
                        )

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = archive.with_name(f"{archive.name}.sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
