from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from pathlib import Path

from continuum.image import load_image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("stdout")
    args = parser.parse_args()
    with Path(args.stdout).open("w", encoding="utf-8") as output:
        with redirect_stdout(output):
            load_image(args.image).restore_vm().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
