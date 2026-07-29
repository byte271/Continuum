from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from continuum.compiler import compile_source
from continuum.errors import ImageError, UnsupportedObjectError
from continuum.image import load_image, save_image, verify_image
from continuum.vm import VirtualMachine


SOURCE = """
def work(limit):
    index = 0
    values = []
    while index < limit:
        values.append(index)
        index += 1
    return len(values)

answer = work(10)
"""


def make_live_image(root: Path) -> Path:
    image = root / "valid.cont"
    vm = VirtualMachine(
        compile_source(SOURCE, "image_test.py"),
        ["image_test.py"],
        "image_test.py",
    )
    while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 4:
        vm.step()
    save_image(image, vm, SOURCE)
    return image


def rewrite_archive(source: Path, target: Path, transform):
    with zipfile.ZipFile(source, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    transform(entries)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def replace_json_and_rehash(entries, name, document):
    entries[name] = json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode()
    checksums = json.loads(entries["checksums.json"])
    checksums["entries"][name] = hashlib.sha256(entries[name]).hexdigest()
    entries["checksums.json"] = json.dumps(
        checksums, sort_keys=True, separators=(",", ":")
    ).encode()


class ImageTests(unittest.TestCase):
    def test_new_image_declares_only_supported_platform_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_live_image(Path(temporary))
            loaded = load_image(image)

        self.assertIn(
            {"os": "Windows", "architecture": "x86_64"},
            loaded.manifest["target_compatibility"]["platforms"],
        )
        self.assertNotIn(
            {"os": "Windows", "architecture": "arm64"},
            loaded.manifest["target_compatibility"]["platforms"],
        )

    def test_unsupported_windows_arm64_pair_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_live_image(Path(temporary))
            loaded = load_image(image)
            with (
                patch("continuum.image.platform.system", return_value="Windows"),
                patch(
                    "continuum.image._normalized_architecture",
                    return_value="arm64",
                ),
            ):
                with self.assertRaisesRegex(
                    ImageError, "target platform is unsupported"
                ):
                    loaded.validate_compatibility()

    def test_verify_deeply_checks_image_without_executing_program(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_live_image(Path(temporary))
            with (
                patch(
                    "continuum.vm.VirtualMachine.run",
                    side_effect=AssertionError("image execution attempted"),
                ),
                patch(
                    "continuum.compiler.compile_source",
                    side_effect=AssertionError("source recompilation attempted"),
                ),
            ):
                report = verify_image(image)
            self.assertEqual(report["integrity"], "verified")
            self.assertEqual(report["graph"], "verified")
            self.assertEqual(report["frames"], "verified")
            self.assertEqual(report["resources"], "metadata-verified-not-opened")

    def test_verify_rejects_invalid_graph_with_valid_archive_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            altered = root / "invalid-graph.cont"

            def alter(entries):
                heap = json.loads(entries["heap/objects.json"])
                heap["root"] = {"t": "ref", "id": len(heap["objects"]) + 10}
                replace_json_and_rehash(
                    entries, "heap/objects.json", heap
                )

            rewrite_archive(image, altered, alter)
            with self.assertRaisesRegex(ImageError, "invalid heap reference"):
                verify_image(altered)

    def test_verify_rejects_frame_metadata_that_disagrees_with_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            altered = root / "invalid-frames.cont"

            def alter(entries):
                frames = json.loads(entries["frames/frames.json"])
                frames["frames"][0]["operand_stack_depth"] += 1
                replace_json_and_rehash(
                    entries, "frames/frames.json", frames
                )

            rewrite_archive(image, altered, alter)
            with self.assertRaisesRegex(ImageError, "frame metadata"):
                verify_image(altered)

    def test_corrupted_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            corrupt = root / "corrupt.cont"

            def alter(entries):
                entries["heap/objects.json"] += b" "

            rewrite_archive(image, corrupt, alter)
            with self.assertRaisesRegex(ImageError, "integrity check failed"):
                load_image(corrupt)

    def test_incompatible_runtime_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            incompatible = root / "incompatible.cont"

            def alter(entries):
                manifest = json.loads(entries["manifest.json"])
                manifest["target_compatibility"]["python_version"] = "0.0.0"
                replace_json_and_rehash(
                    entries, "manifest.json", manifest
                )

            rewrite_archive(image, incompatible, alter)
            with self.assertRaisesRegex(ImageError, "runtime metadata is inconsistent"):
                load_image(incompatible)

    def test_image_has_no_native_executable_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = make_live_image(Path(temporary))
            with zipfile.ZipFile(image, "r") as archive:
                for name in archive.namelist():
                    content = archive.read(name)
                    self.assertFalse(content.startswith(b"\x7fELF"), name)
                    self.assertFalse(content.startswith(b"\xcf\xfa\xed\xfe"), name)
                    self.assertNotIn(Path(name).suffix, {".so", ".dylib", ".dll"})

    def test_unsupported_live_object_aborts_checkpoint(self):
        source = """
import hashlib
token = hashlib.sha256()
index = 0
while index < 10:
    index += 1
"""
        with tempfile.TemporaryDirectory() as temporary:
            vm = VirtualMachine(
                compile_source(source, "unsupported.py"),
                ["unsupported.py"],
                "unsupported.py",
            )
            while "token" not in vm.globals:
                vm.step()
            with self.assertRaisesRegex(
                UnsupportedObjectError, "unsupported live object"
            ):
                save_image(Path(temporary) / "bad.cont", vm, source)

    def test_altered_ir_source_identity_is_rejected_even_with_valid_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            altered = root / "altered-ir.cont"

            def alter(entries):
                ir = json.loads(entries["code/ir.json"])
                ir["source_sha256"] = "0" * 64
                replace_json_and_rehash(entries, "code/ir.json", ir)
                modules = json.loads(entries["modules/hashes.json"])
                modules["continuum_ir"] = hashlib.sha256(
                    entries["code/ir.json"]
                ).hexdigest()
                replace_json_and_rehash(entries, "modules/hashes.json", modules)

            rewrite_archive(image, altered, alter)
            with self.assertRaisesRegex(ImageError, "IR source identity"):
                load_image(altered)

    def test_unknown_mandatory_capability_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            altered = root / "unknown-capability.cont"

            def alter(entries):
                manifest = json.loads(entries["manifest.json"])
                manifest["target_compatibility"]["required_capabilities"].append(
                    "execute-native-pointer-table"
                )
                replace_json_and_rehash(entries, "manifest.json", manifest)

            rewrite_archive(image, altered, alter)
            with self.assertRaisesRegex(ImageError, "unknown mandatory"):
                load_image(altered)

    def test_truncated_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = make_live_image(root)
            truncated = root / "truncated.cont"
            content = image.read_bytes()
            truncated.write_bytes(content[: len(content) // 2])
            with self.assertRaisesRegex(ImageError, "valid Continuum image"):
                load_image(truncated)

    def test_resource_metadata_missing_reconstruction_field_is_rejected(self):
        source = """
handle = open(__args__[1], "r")
first = handle.readline()
index = 0
while index < 10:
    index += 1
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data.txt"
            data.write_text("one\\ntwo\\n", encoding="utf-8")
            vm = VirtualMachine(
                compile_source(source, "resource_attack.py"),
                ["resource_attack.py", str(data)],
                "resource_attack.py",
                resource_policy="bundle",
            )
            while vm.frames[-1].locals.get("index") != 3:
                vm.step()
            image = root / "resource.cont"
            save_image(image, vm, source)
            altered = root / "bad-resource.cont"

            def alter(entries):
                resources = json.loads(entries["resources/resources.json"])
                del resources["resources"][0]["encoding"]
                replace_json_and_rehash(
                    entries, "resources/resources.json", resources
                )

            rewrite_archive(image, altered, alter)
            with self.assertRaisesRegex(ImageError, "resource record is missing"):
                load_image(altered)
            for resource in vm.resources.files.values():
                resource.close()


if __name__ == "__main__":
    unittest.main()
