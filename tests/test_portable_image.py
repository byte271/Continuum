from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from continuum.compiler import compile_source
from continuum.errors import ImageError
from continuum.image import load_image
from continuum.portable_image import (
    EXECUTION_ABI_CAPABILITY,
    EXECUTION_ABI_VERSION,
    SUPPORTED_PYTHON_VERSIONS,
    load_portable_image,
    save_portable_image,
    verify_portable_image,
)
from continuum.vm import VirtualMachine


SOURCE = """
def work(limit):
    index = 0
    total = 0
    while index < limit:
        total += index
        index += 1
    return total

answer = work(20)
"""


def make_image(root: Path) -> Path:
    vm = VirtualMachine(
        compile_source(SOURCE, "portable_test.py"),
        ["portable_test.py"],
        "portable_test.py",
    )
    while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 8:
        vm.step()
    image = root / "portable.cont"
    save_portable_image(image, vm, SOURCE)
    return image


class PortableImageTests(unittest.TestCase):
    def test_writer_declares_execution_abi_and_python_version_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_image(Path(temporary))
            loaded = load_portable_image(image)

        compatibility = loaded.manifest["target_compatibility"]
        self.assertEqual(compatibility["execution_abi"], EXECUTION_ABI_VERSION)
        self.assertEqual(
            compatibility["python_versions"], list(SUPPORTED_PYTHON_VERSIONS)
        )
        self.assertIn(
            EXECUTION_ABI_CAPABILITY, compatibility["required_capabilities"]
        )
        self.assertEqual(
            compatibility["runtime_version_policy"], "execution-abi"
        )

    def test_shipping_exact_version_reader_refuses_portable_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_image(Path(temporary))
            with self.assertRaisesRegex(ImageError, "unknown mandatory"):
                load_image(image)

    def test_verified_target_python_is_accepted_without_runtime_version_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_image(root)
            altered = root / "future-runtime.cont"
            with zipfile.ZipFile(image, "r") as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            manifest = json.loads(entries["manifest.json"])
            runtime = json.loads(entries["runtime.json"])
            manifest["target_compatibility"]["runtime_version"] = "99.0.0"
            runtime["runtime_version"] = "99.0.0"
            entries["manifest.json"] = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            entries["runtime.json"] = json.dumps(
                runtime, sort_keys=True, separators=(",", ":")
            ).encode()
            import hashlib

            checksums = {
                "algorithm": "sha256",
                "entries": {
                    name: hashlib.sha256(content).hexdigest()
                    for name, content in entries.items()
                    if name not in {"checksums.json", "SIGNATURE"}
                },
            }
            entries["checksums.json"] = json.dumps(
                checksums, sort_keys=True, separators=(",", ":")
            ).encode()
            with zipfile.ZipFile(altered, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)

            with patch(
                "continuum.portable_image._runtime_python",
                return_value="3.13.14",
            ):
                loaded = load_portable_image(altered)
                loaded.validate_compatibility()
                vm = loaded.restore_vm()
                result = vm.run()

        self.assertIsNone(result)
        self.assertEqual(vm.globals["answer"], sum(range(20)))

    def test_unverified_python_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_image(Path(temporary))
            with patch(
                "continuum.portable_image._runtime_python",
                return_value="3.14.6",
            ):
                loaded = load_portable_image(image)
                with self.assertRaisesRegex(ImageError, "not accepted"):
                    loaded.validate_compatibility()

    def test_deep_verification_reports_cross_python_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_image(Path(temporary))
            with patch(
                "continuum.portable_image._runtime_python",
                return_value="3.13.14",
            ):
                report = verify_portable_image(image)

        self.assertEqual(report["execution_abi"], EXECUTION_ABI_VERSION)
        self.assertEqual(report["target_python"], "3.13.14")
        self.assertEqual(report["frames"], "verified")


if __name__ == "__main__":
    unittest.main()
