"""Guard the manifest keys the cross-platform proof harness reads.

The proof workflow runs only on push to `main`, never on `pull_request`, so a
harness that stops understanding the image format is invisible to every pull
request check. That is exactly what happened: `source_linux.py` read
`native_payload_required` from its format 0.1 location only, and every format
0.2 image made the source job die with `KeyError: 'target_compatibility'`.

These tests read the flag out of an image this repository actually produces, so
the guard tracks the writer rather than a copy of it.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]

# `source_linux.py` is run as a script by the workflow and imports its siblings
# by bare name (`from common import ...`), so importing it as a module needs the
# same directory on `sys.path` that running it as a script would provide.
_HARNESS = REPOSITORY / "validation" / "cross_platform"
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))

from continuum.compiler import compile_source  # noqa: E402
from continuum.image import save_image  # noqa: E402
from continuum.vm import VirtualMachine  # noqa: E402
from validation.cross_platform.source_linux import (  # noqa: E402
    native_payload_required,
)

SOURCE = """
def inner(limit, bag):
    index = 0
    while index < limit:
        bag.append(index)
        print(f"WORK {index}")
        index += 1
    return len(bag)


def outer(limit):
    bag = []
    return inner(limit, bag)


answer = outer(25)
"""


def written_manifest() -> dict[str, object]:
    """Freeze a real image and return the manifest the writer actually wrote."""

    name = "manifest_read.py"
    vm = VirtualMachine(compile_source(SOURCE, name), [name], name)
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 6:
            vm.step()
    with tempfile.TemporaryDirectory() as directory:
        image = Path(directory) / "manifest_read.cont"
        save_image(image, vm, SOURCE)
        with zipfile.ZipFile(image) as archive:
            return json.loads(archive.read("manifest.json"))


class NativePayloadRequiredTests(unittest.TestCase):
    def test_reads_the_flag_from_an_image_this_repository_writes(self):
        manifest = written_manifest()
        self.assertEqual(manifest["format_version"], "0.2")
        self.assertIs(native_payload_required(manifest), False)

    def test_current_format_does_not_carry_the_legacy_section(self):
        # The regression this file guards was not a renamed key inside a section
        # that survived -- the whole 0.1 section is gone, so the old read could
        # only ever raise. If a future format restores `target_compatibility`,
        # this failing is the signal to re-check which location wins below.
        self.assertNotIn("target_compatibility", written_manifest())

    def test_reads_the_flag_from_a_legacy_format_manifest(self):
        manifest = {
            "format_version": "0.1",
            "target_compatibility": {
                "runtime_implementation": "continuum-vm",
                "native_payload_required": False,
                "required_capabilities": [],
            },
        }
        self.assertIs(native_payload_required(manifest), False)

    def test_reports_a_legacy_image_that_does_require_a_native_payload(self):
        # Without this the tests above are also satisfied by a function that
        # returns False unconditionally.
        manifest = {
            "format_version": "0.1",
            "target_compatibility": {"native_payload_required": True},
        }
        self.assertIs(native_payload_required(manifest), True)

    def test_contract_location_wins_when_a_manifest_carries_both(self):
        manifest = {
            "format_version": "0.2",
            "execution_contract": {"target": {"native_payload_required": False}},
            "target_compatibility": {"native_payload_required": True},
        }
        self.assertIs(native_payload_required(manifest), False)

    def test_a_manifest_with_neither_location_raises_instead_of_defaulting(self):
        # Returning False here would record "this image needs no native payload"
        # as proven evidence, which the macOS job then reads back as a portability
        # result. Nothing established it, so the harness must refuse to say it.
        manifest = {"format_version": "0.3", "execution_contract": {"target": {}}}
        with self.assertRaises(RuntimeError) as caught:
            native_payload_required(manifest)
        self.assertIn("0.3", str(caught.exception))

    def test_a_non_boolean_flag_is_refused_rather_than_coerced(self):
        # `target_macos.py` compares the recorded value with `is False`. Coercing
        # 0 to False would pass that check for an image the runtime itself
        # refuses, since `abi.parse_contract` demands the literal False.
        manifest = {
            "format_version": "0.2",
            "execution_contract": {"target": {"native_payload_required": 0}},
        }
        with self.assertRaises(RuntimeError):
            native_payload_required(manifest)


if __name__ == "__main__":
    unittest.main()
