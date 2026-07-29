#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

from common import (
    sha256_file,
    verify_file_manifest,
    verify_repository_identity,
)


def verify_source(directory: Path) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    manifest_hash = verify_file_manifest(directory, "linux-evidence.sha256")
    evidence = json.loads(
        (directory / "source-evidence.json").read_text(encoding="utf-8")
    )
    if not evidence.get("qualified_native_linux_x86_64"):
        raise RuntimeError("source evidence is not qualified native Linux x86_64")
    if evidence.get("rehearsal"):
        raise RuntimeError("source evidence is marked as a rehearsal")
    if not (
        evidence.get("source_process_exited")
        and evidence.get("source_process_reaped")
        and evidence.get("source_returncode") == 0
    ):
        raise RuntimeError("source exit and reap evidence is incomplete")
    if not evidence.get("deleted_original_input"):
        raise RuntimeError("original bundled input deletion is not proven")
    image = directory / str(evidence["image"])
    if sha256_file(image) != evidence["image_sha256"]:
        raise RuntimeError("source image SHA-256 mismatch")
    if stat.S_IMODE(image.stat().st_mode) != 0o444:
        raise RuntimeError("source image is not read-only")
    identity = verify_repository_identity(repository, directory)
    if identity["git_commit"] != evidence["git_commit"]:
        raise RuntimeError("source Git identity does not match evidence")
    return {
        "phase": "source-verification",
        "git_commit": evidence["git_commit"],
        "image_sha256": evidence["image_sha256"],
        "linux_evidence_manifest_sha256": manifest_hash,
    }


def verify_final(directory: Path) -> dict[str, object]:
    manifest_hash = verify_file_manifest(directory, "final-evidence.sha256")
    comparison = json.loads(
        (directory / "comparison.json").read_text(encoding="utf-8")
    )
    if not comparison.get("all_success_conditions_pass"):
        raise RuntimeError("not all cross-platform success conditions passed")
    conditions = {
        key: value
        for key, value in comparison.items()
        if key.startswith("condition_")
    }
    if len(conditions) != 26 or not all(conditions.values()):
        raise RuntimeError("the final comparison does not contain 26 passing conditions")
    return {
        "phase": "final-verification",
        "condition_count": len(conditions),
        "final_evidence_manifest_sha256": manifest_hash,
        "image_sha256": comparison["image_sha256_target_after"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("source", "final"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        if args.phase == "source":
            result = verify_source(directory)
        else:
            result = verify_final(directory)
    except BaseException as exc:
        print(f"evidence verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
