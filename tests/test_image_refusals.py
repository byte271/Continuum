"""Deterministic refusals at the image boundary.

An image is executable untrusted content. Every case here builds a real image,
tampers with exactly one thing, recomputes every archive checksum so the
tampering is internally consistent, and asserts the image is still refused.

Recomputing the checksums is the point. An attacker who edits a manifest will
also fix the hashes, so integrity checking alone proves nothing about metadata
that must agree with the rest of the image. These tests fail if the runtime ever
starts trusting a well-formed lie.

Nothing here executes the frozen program.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from continuum import IR_VERSION, __version__, abi
from continuum.abi import (
    CONTAINER_FORMAT_VERSION,
    LEGACY_CONTAINER_FORMAT_VERSION,
    Host,
    IncompatibleImage,
)
from continuum.compiler import compile_source
from continuum.errors import ImageError
from continuum.image import load_image, save_image, verify_image
from continuum.vm import VirtualMachine

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
    shared = {"a": bag, "b": bag}
    shared["self"] = shared
    total = inner(limit, bag)
    print(f"DONE {total}")
    return total


answer = outer(25)
"""


def other_verified_python() -> str:
    """A verified interpreter version that is not the one running the tests.

    Derived rather than hard-coded: a literal would silently become a no-op
    whenever the suite happens to run on that exact interpreter, which would
    turn a refusal test into a test that asserts nothing.
    """

    import platform

    current = platform.python_version()
    for version in abi.VERIFIED_PYTHON_VERSIONS:
        if version != current:
            return version
    raise AssertionError("no second verified Python version to contrast against")


# A version this runtime will never accept, for cases that only need the
# creator provenance to disagree with the rest of the image.
UNVERIFIED_PYTHON = "3.7.99"


def _json_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def step_to_checkpoint(source: str, name: str, sentinel: int) -> VirtualMachine:
    """Advance a fresh VM to a live checkpoint, without leaking its output."""

    vm = VirtualMachine(compile_source(source, name), [name], name)
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != sentinel:
            vm.step()
    return vm


def make_image(root: Path, name: str = "valid.cont") -> Path:
    image = root / name
    vm = step_to_checkpoint(SOURCE, "refusal_test.py", 6)
    save_image(image, vm, SOURCE)
    return image


def rewrite(source: Path, target: Path, transform) -> Path:
    """Rewrite an archive, then recompute every covered checksum.

    The result is an image whose integrity document is fully correct for its
    tampered contents -- the situation a real attacker produces.
    """

    with zipfile.ZipFile(source, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    transform(entries)
    covered = {
        name: _sha256(content)
        for name, content in sorted(entries.items())
        if name not in {"checksums.json", "SIGNATURE"}
    }
    entries["checksums.json"] = _json_bytes(
        {"algorithm": "sha256", "entries": covered}
    )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    return target


def patch_manifest(entries: dict[str, bytes], mutate) -> None:
    manifest = json.loads(entries["manifest.json"])
    mutate(manifest)
    entries["manifest.json"] = _json_bytes(manifest)


def patch_runtime(entries: dict[str, bytes], mutate) -> None:
    runtime = json.loads(entries["runtime.json"])
    mutate(runtime)
    entries["runtime.json"] = _json_bytes(runtime)


class ImageRefusalCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.image = make_image(self.root)

    def tearDown(self):
        self._temporary.cleanup()

    def tampered(self, transform, name="tampered.cont") -> Path:
        return rewrite(self.image, self.root / name, transform)

    def assertLoadRefused(self, transform, pattern):
        target = self.tampered(transform)
        with self.assertRaisesRegex(ImageError, pattern):
            load_image(target)

    def assertCompatibilityRefused(self, transform, reason, host=None):
        target = self.tampered(transform)
        loaded = load_image(target)
        with self.assertRaises(IncompatibleImage) as caught:
            loaded.validate_compatibility(host)
        self.assertEqual(caught.exception.reason, reason)


class ContractVersionRefusalTests(ImageRefusalCase):
    def test_unknown_execution_abi_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"].update(
                    execution_abi_version="99.0"
                ),
            )
            patch_runtime(entries, lambda r: r.update(execution_abi_version="99.0"))

        self.assertCompatibilityRefused(
            transform, abi.REASON_UNKNOWN_EXECUTION_ABI
        )

    def test_unknown_ir_version_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries, lambda m: m["execution_contract"].update(ir_version="99.0")
            )

        self.assertLoadRefused(transform, "runtime metadata is inconsistent")

    def test_unknown_graph_codec_version_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"].update(graph_codec_version="99.0"),
            )
            patch_runtime(entries, lambda r: r.update(graph_codec_version="99.0"))

        self.assertCompatibilityRefused(transform, abi.REASON_UNKNOWN_GRAPH_CODEC)

    def test_unknown_container_format_is_refused(self):
        def transform(entries):
            patch_manifest(entries, lambda m: m.update(format_version="99.0"))

        self.assertLoadRefused(transform, "unsupported image format version")

    def test_a_contract_claiming_the_legacy_policy_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"].update(
                    compatibility_policy=abi.POLICY_EXACT
                ),
            )

        self.assertCompatibilityRefused(transform, abi.REASON_POLICY_DOWNGRADE)

    def test_an_unknown_policy_name_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"].update(
                    compatibility_policy="whatever-you-say"
                ),
            )

        self.assertLoadRefused(transform, "unknown compatibility policy")


class PythonAllowlistRefusalTests(ImageRefusalCase):
    def test_an_unverified_python_version_is_refused(self):
        """Widening the image allowlist does not widen what the runtime accepts."""

        def transform(entries):
            def mutate(manifest):
                contract = manifest["execution_contract"]
                contract["target"]["python_versions"] = ["3.12.13", "3.99.0"]

            patch_manifest(entries, mutate)

        self.assertCompatibilityRefused(
            transform,
            abi.REASON_PYTHON_NOT_VERIFIED_BY_RUNTIME,
            host=Host("3.99.0", "Linux", "x86_64"),
        )

    def test_a_python_outside_the_image_allowlist_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["target"].update(
                    python_versions=["3.12.13"]
                ),
            )

        self.assertCompatibilityRefused(
            transform,
            abi.REASON_PYTHON_NOT_IN_IMAGE_ALLOWLIST,
            host=Host("3.13.14", "Linux", "x86_64"),
        )

    def test_a_malformed_allowlist_is_refused(self):
        for value in ([], ["3.12.13", "3.12.13"], "3.12.13", ["3.12.13", 313]):
            with self.subTest(value=value):

                def transform(entries, value=value):
                    patch_manifest(
                        entries,
                        lambda m: m["execution_contract"]["target"].update(
                            python_versions=value
                        ),
                    )

                self.assertLoadRefused(
                    transform, "target Python allowlist is malformed"
                )

    def test_an_empty_capability_list_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["target"].update(
                    required_capabilities=[]
                ),
            )

        self.assertLoadRefused(transform, "is not a valid list")


class CapabilityRefusalTests(ImageRefusalCase):
    def test_a_capability_this_runtime_lacks_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["target"][
                    "required_capabilities"
                ].append("native-pointer-table-1.0"),
            )

        self.assertCompatibilityRefused(transform, abi.REASON_MISSING_CAPABILITY)

    def test_omitting_a_mandatory_capability_is_refused(self):
        def transform(entries):
            def mutate(manifest):
                target = manifest["execution_contract"]["target"]
                target["required_capabilities"] = [
                    item
                    for item in target["required_capabilities"]
                    if item != "explicit-frames"
                ]

            patch_manifest(entries, mutate)

        self.assertCompatibilityRefused(transform, abi.REASON_UNKNOWN_CAPABILITY)

    def test_requiring_a_native_payload_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["target"].update(
                    native_payload_required=True
                ),
            )

        self.assertLoadRefused(transform, "requires a native payload")


class ProvenanceConsistencyTests(ImageRefusalCase):
    """Creator metadata is provenance, so it must still be internally true."""

    def test_rewritten_creator_python_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["creator"].update(
                    python_version=UNVERIFIED_PYTHON
                ),
            )

        self.assertLoadRefused(transform, "creator Python provenance disagrees")

    def test_rewritten_creator_runtime_version_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["creator"].update(
                    continuum_version="9.9.9"
                ),
            )

        self.assertLoadRefused(transform, "creator runtime provenance disagrees")

    def test_rewritten_creator_platform_is_refused(self):
        def transform(entries):
            patch_manifest(
                entries,
                lambda m: m["execution_contract"]["creator"].update(os="Plan9"),
            )

        self.assertLoadRefused(transform, "creator platform provenance disagrees")

    def test_rewriting_both_creator_and_source_sections_is_still_refused(self):
        """Fixing one half of the inconsistency is not enough."""

        def transform(entries):
            def mutate(manifest):
                manifest["execution_contract"]["creator"][
                    "python_version"
                ] = UNVERIFIED_PYTHON
                manifest["source"]["python_version"] = UNVERIFIED_PYTHON

            patch_manifest(entries, mutate)

        # The manifest now agrees with itself, so runtime.json is what catches it.
        self.assertLoadRefused(transform, "creator Python provenance disagrees")

    def test_rewriting_manifest_and_runtime_together_is_caught_by_the_allowlist(self):
        """Making every document agree still cannot invent a creator identity.

        With the manifest, its source section, and runtime.json all rewritten,
        the remaining check is that the creator version appears in the image's
        own target allowlist.
        """

        def transform(entries):
            def mutate_manifest(manifest):
                contract = manifest["execution_contract"]
                contract["creator"]["python_version"] = "3.7.0"
                contract["target"]["python_versions"] = ["3.12.13", "3.13.14"]
                manifest["source"]["python_version"] = "3.7.0"

            patch_manifest(entries, mutate_manifest)
            patch_runtime(entries, lambda r: r.update(python_version="3.7.0"))

        self.assertCompatibilityRefused(
            transform, abi.REASON_INCONSISTENT_PROVENANCE
        )

    def test_execution_abi_disagreement_between_documents_is_refused(self):
        def transform(entries):
            patch_runtime(entries, lambda r: r.update(execution_abi_version="0.9"))

        self.assertLoadRefused(transform, "execution ABI metadata is inconsistent")

    def test_graph_codec_disagreement_between_documents_is_refused(self):
        def transform(entries):
            patch_runtime(entries, lambda r: r.update(graph_codec_version="0.9"))

        self.assertLoadRefused(transform, "graph codec metadata is inconsistent")


class StructuralRefusalTests(ImageRefusalCase):
    def test_a_missing_contract_is_refused(self):
        def transform(entries):
            patch_manifest(entries, lambda m: m.pop("execution_contract"))

        self.assertLoadRefused(transform, "execution contract is not an object")

    def test_a_contract_that_is_not_an_object_is_refused(self):
        def transform(entries):
            patch_manifest(entries, lambda m: m.update(execution_contract=[1, 2]))

        self.assertLoadRefused(transform, "execution contract is not an object")

    def test_a_tampered_program_body_is_refused(self):
        """Editing the frozen source is refused even with correct checksums."""

        def transform(entries):
            entries["code/program.py"] = SOURCE.replace(
                "WORK", "TAMPERED"
            ).encode("utf-8")

        self.assertLoadRefused(transform, "program hash does not match manifest")

    def test_a_tampered_ir_document_is_refused(self):
        def transform(entries):
            ir = json.loads(entries["code/ir.json"])
            ir["source_sha256"] = "0" * 64
            entries["code/ir.json"] = _json_bytes(ir)

        self.assertLoadRefused(transform, "IR source identity does not match manifest")

    def test_a_frame_count_disagreement_is_refused(self):
        def transform(entries):
            patch_manifest(entries, lambda m: m.update(frames=99))

        self.assertLoadRefused(transform, "frame count does not match manifest")

    def test_a_heap_count_disagreement_is_refused(self):
        def transform(entries):
            patch_manifest(entries, lambda m: m.update(heap_objects=99999))

        self.assertLoadRefused(transform, "heap object count does not match manifest")

    def test_an_invalid_frame_document_is_refused(self):
        def transform(entries):
            entries["frames/frames.json"] = _json_bytes({"frames": "not-a-list"})

        self.assertLoadRefused(transform, "invalid frame metadata")

    def test_a_dropped_security_boundary_is_refused(self):
        def transform(entries):
            patch_manifest(entries, lambda m: m.pop("security_boundary"))

        self.assertLoadRefused(
            transform, "omits its executable-content security boundary"
        )

    def test_a_broken_checksum_is_refused(self):
        """Without recomputing hashes, ordinary integrity checking catches it."""

        with zipfile.ZipFile(self.image, "r") as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries["heap/objects.json"] = _json_bytes({"objects": []})
        broken = self.root / "broken.cont"
        with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(name, content)
        with self.assertRaisesRegex(ImageError, "integrity check failed"):
            load_image(broken)

    def test_a_duplicate_archive_entry_is_refused(self):
        duplicate = self.root / "duplicate.cont"
        with zipfile.ZipFile(self.image, "r") as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        with zipfile.ZipFile(duplicate, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(name, content)
            # Writing the name twice is the point of the fixture, and CPython
            # warns about it. Scoped here so the warning cannot be mistaken for
            # one the runtime emitted, and so a real warning still stands out.
            with warnings.catch_warnings():
                # Matched on message, not category alone: a blanket UserWarning
                # filter would also swallow an unrelated future warning from
                # this same write, which is the opposite of the intent.
                warnings.filterwarnings(
                    "ignore", category=UserWarning, message=r"Duplicate name: "
                )
                archive.writestr("manifest.json", entries["manifest.json"])
        with self.assertRaises(ImageError):
            load_image(duplicate)


class LegacyFormatTests(unittest.TestCase):
    """Format 0.1 images keep the rule they were proven under."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.image = make_image(self.root)

    def tearDown(self):
        self._temporary.cleanup()

    def legacy_image(self) -> Path:
        """Convert a 0.2 image into an equivalent 0.1 image.

        This synthesizes the shape a 0.3.1 writer produced, so the legacy path
        is exercised end to end rather than only unit-tested.
        """

        def transform(entries):
            def mutate(manifest):
                contract = manifest["execution_contract"]
                manifest["format_version"] = LEGACY_CONTAINER_FORMAT_VERSION
                manifest["target_compatibility"] = {
                    "operating_systems": list(contract["target"]["operating_systems"]),
                    "architectures": list(contract["target"]["architectures"]),
                    "platforms": [
                        dict(entry) for entry in contract["target"]["platforms"]
                    ],
                    "python_version": contract["creator"]["python_version"],
                    "runtime_implementation": "continuum-vm",
                    "runtime_version": contract["creator"]["continuum_version"],
                    "native_payload_required": False,
                    "required_capabilities": sorted(
                        contract["target"]["required_capabilities"]
                    ),
                }
                manifest.pop("execution_contract")

            patch_manifest(entries, mutate)

        return rewrite(self.image, self.root / "legacy.cont", transform)

    def test_a_legacy_image_still_loads(self):
        loaded = load_image(self.legacy_image())
        self.assertEqual(
            loaded.manifest["format_version"], LEGACY_CONTAINER_FORMAT_VERSION
        )

    def test_a_legacy_image_is_accepted_on_its_exact_creator_host(self):
        loaded = load_image(self.legacy_image())
        decision = loaded.validate_compatibility(
            Host(
                loaded.manifest["source"]["python_version"],
                "Linux",
                "x86_64",
                continuum_version=__version__,
            )
        )
        self.assertEqual(decision["compatibility_policy"], abi.POLICY_EXACT)

    def test_a_legacy_image_refuses_a_different_python_with_a_versioned_message(self):
        loaded = load_image(self.legacy_image())
        with self.assertRaises(IncompatibleImage) as caught:
            loaded.validate_compatibility(
                Host(
                    other_verified_python(),
                    "Linux",
                    "x86_64",
                    continuum_version=__version__,
                )
            )
        self.assertEqual(
            caught.exception.reason, abi.REASON_LEGACY_PYTHON_MISMATCH
        )
        message = str(caught.exception)
        # The message must name both formats and say what to do about it.
        self.assertIn(LEGACY_CONTAINER_FORMAT_VERSION, message)
        self.assertIn(CONTAINER_FORMAT_VERSION, message)
        self.assertIn("Re-freeze", message)

    def test_a_legacy_image_refuses_a_different_runtime_version(self):
        loaded = load_image(self.legacy_image())
        with self.assertRaises(IncompatibleImage) as caught:
            loaded.validate_compatibility(
                Host(
                    loaded.manifest["source"]["python_version"],
                    "Linux",
                    "x86_64",
                    continuum_version="0.9.9",
                )
            )
        self.assertEqual(
            caught.exception.reason, abi.REASON_LEGACY_RUNTIME_MISMATCH
        )

    def test_a_legacy_image_cannot_smuggle_in_a_contract_policy(self):
        """Declaring 0.1 while carrying a contract does not get the ABI rule."""

        def transform(entries):
            def mutate(manifest):
                manifest["format_version"] = LEGACY_CONTAINER_FORMAT_VERSION
                manifest["target_compatibility"] = {
                    "operating_systems": ["Linux"],
                    "architectures": ["x86_64"],
                    "platforms": [{"os": "Linux", "architecture": "x86_64"}],
                    "python_version": other_verified_python(),
                    "runtime_implementation": "continuum-vm",
                    "runtime_version": __version__,
                    "native_payload_required": False,
                    "required_capabilities": sorted(
                        manifest["execution_contract"]["target"][
                            "required_capabilities"
                        ]
                    ),
                }

            patch_manifest(entries, mutate)

        target = rewrite(self.image, self.root / "smuggled.cont", transform)
        # runtime.json still records the real creator Python, so the legacy
        # consistency check refuses the mismatch before any policy is applied.
        with self.assertRaisesRegex(ImageError, "runtime metadata is inconsistent"):
            load_image(target)


class VerificationDoesNotExecuteTests(unittest.TestCase):
    def test_verify_reconstructs_state_without_running_the_program(self):
        marker = "SIDE-EFFECT-MUST-NOT-HAPPEN"
        source = f"""
def work(limit):
    index = 0
    while index < limit:
        index += 1
        print("STEP")
    print("{marker}")
    return index


answer = work(20)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "verify.cont"
            vm = step_to_checkpoint(source, "verify_test.py", 3)
            save_image(image, vm, source)

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                report = verify_image(image)

        self.assertEqual(report["compatibility"], "accepted")
        self.assertEqual(report["execution_contract"]["compatibility_policy"], "execution-abi")
        # Neither the loop body nor the trailing marker may run.
        self.assertNotIn(marker, captured.getvalue())
        self.assertNotIn("STEP", captured.getvalue())




class AdversarialPlatformWideningTests(ImageRefusalCase):
    """An image cannot grant itself a platform this runtime does not accept.

    This is the whole-image version of the contract-level gate: a real image is
    edited to add Windows arm64 to every platform list it carries, and every
    covered archive checksum is recomputed so the artifact is internally
    consistent. Integrity checking passes and the image still must not restore.
    """

    def widened(self, name: str = "windows-arm64.cont") -> Path:
        def transform(entries):
            def mutate(manifest):
                target = manifest["execution_contract"]["target"]
                target["operating_systems"] = sorted(
                    set(target["operating_systems"]) | {"Windows"}
                )
                target["architectures"] = sorted(
                    set(target["architectures"]) | {"arm64"}
                )
                target["platforms"] = target["platforms"] + [
                    {"os": "Windows", "architecture": "arm64"}
                ]

            patch_manifest(entries, mutate)

        return rewrite(self.image, self.root / name, transform)

    def test_the_tampered_image_is_internally_consistent(self):
        """Guard the premise: this must fail on policy, not on a broken hash."""
        loaded = load_image(self.widened("premise.cont"))
        platforms = loaded.manifest["execution_contract"]["target"]["platforms"]
        self.assertIn({"os": "Windows", "architecture": "arm64"}, platforms)

    def test_windows_arm64_is_refused_deterministically(self):
        loaded = load_image(self.widened())
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                with self.assertRaises(IncompatibleImage) as caught:
                    loaded.validate_compatibility(
                        Host("3.12.13", "Windows", "arm64")
                    )
                self.assertEqual(
                    caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM
                )
                self.assertIn(
                    "this runtime does not accept platform", str(caught.exception)
                )

    def test_the_refusal_survives_a_verified_python_version(self):
        """Widening the platform cannot be smuggled in on a verified interpreter."""
        loaded = load_image(self.widened("with-verified-python.cont"))
        for version in abi.VERIFIED_PYTHON_VERSIONS:
            with self.subTest(python=version):
                with self.assertRaises(IncompatibleImage) as caught:
                    loaded.validate_compatibility(
                        Host(version, "Windows", "arm64")
                    )
                self.assertEqual(
                    caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM
                )

    def test_the_widened_image_still_restores_on_an_accepted_platform(self):
        """The tampering must not break unrelated, legitimate targets."""
        loaded = load_image(self.widened("still-good.cont"))
        decision = loaded.validate_compatibility(Host("3.12.13", "Linux", "x86_64"))
        self.assertEqual(decision["compatibility_policy"], abi.POLICY_EXECUTION_ABI)

    def test_verify_image_refuses_the_widened_image_on_that_platform(self):
        """The public deep-verify path refuses it too, not just the decision."""
        from unittest import mock

        target = self.widened("verify-path.cont")
        with mock.patch.object(
            abi, "current_host", return_value=Host("3.12.13", "Windows", "arm64")
        ):
            with self.assertRaises(IncompatibleImage) as caught:
                verify_image(target)
        self.assertEqual(caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM)


if __name__ == "__main__":
    unittest.main()
