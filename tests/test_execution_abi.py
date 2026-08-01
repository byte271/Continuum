"""Acceptance and refusal boundaries of the execution compatibility contract.

Every case here runs on a single interpreter. `Host` is injectable precisely so
that refusals which would otherwise need an unavailable Python version are still
exercised deterministically, and so a passing suite means the policy was tested
rather than the machine it happened to run on.
"""

from __future__ import annotations

import copy
import unittest

from continuum import IR_VERSION, abi
from continuum.abi import Host, IncompatibleImage


LINUX_312 = Host("3.12.13", "Linux", "x86_64")
MACOS_313 = Host("3.13.14", "Darwin", "arm64")


def contract() -> dict:
    return abi.build_contract("Linux", "x86_64", "3.12.13")


class AcceptanceTests(unittest.TestCase):
    def test_creator_host_accepts_its_own_image(self):
        self.assertEqual(
            abi.decide_restore(contract(), LINUX_312)["execution_abi_version"],
            abi.EXECUTION_ABI_VERSION,
        )

    def test_the_cross_python_cross_os_cross_isa_target_is_accepted(self):
        """The exact migration Phase 1 must support, decided by policy alone."""
        accepted = abi.decide_restore(contract(), MACOS_313)
        self.assertEqual(accepted["creator"]["python_version"], "3.12.13")
        self.assertIn("3.13.14", accepted["target"]["python_versions"])

    def test_every_verified_python_version_is_accepted(self):
        for version in abi.VERIFIED_PYTHON_VERSIONS:
            with self.subTest(version=version):
                host = Host(version, "Linux", "x86_64")
                abi.decide_restore(contract(), host)

    def test_a_different_creator_continuum_version_is_still_accepted(self):
        """Creator runtime version is provenance, not a restore requirement."""
        document = contract()
        document["creator"]["continuum_version"] = "0.0.1-something-else"
        accepted = abi.decide_restore(document, MACOS_313)
        self.assertEqual(
            accepted["creator"]["continuum_version"], "0.0.1-something-else"
        )

    def test_creator_provenance_is_preserved_exactly(self):
        document = abi.build_contract("Linux", "x86_64", "3.12.13", "0.4.0a1")
        accepted = abi.decide_restore(document, MACOS_313)
        self.assertEqual(
            accepted["creator"],
            {
                "continuum_version": "0.4.0a1",
                "python_version": "3.12.13",
                "os": "Linux",
                "architecture": "x86_64",
            },
        )


class RefusalTests(unittest.TestCase):
    def assertRefused(self, document, host, reason):
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(document, host)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_unknown_execution_abi_is_refused(self):
        document = contract()
        document["execution_abi_version"] = "2.0"
        document["target"]["required_capabilities"] = sorted(
            set(document["target"]["required_capabilities"])
            - {f"execution-abi-{abi.EXECUTION_ABI_VERSION}"}
            | {"execution-abi-2.0"}
        )
        self.assertRefused(document, LINUX_312, abi.REASON_UNKNOWN_EXECUTION_ABI)

    def test_unknown_ir_version_is_refused(self):
        document = contract()
        document["ir_version"] = "9.9"
        self.assertRefused(document, LINUX_312, abi.REASON_UNKNOWN_IR_VERSION)

    def test_unknown_graph_codec_version_is_refused(self):
        document = contract()
        document["graph_codec_version"] = "9.9"
        self.assertRefused(document, LINUX_312, abi.REASON_UNKNOWN_GRAPH_CODEC)

    def test_unknown_container_format_is_refused(self):
        document = contract()
        document["container_format_version"] = "0.99"
        self.assertRefused(document, LINUX_312, abi.REASON_UNKNOWN_CONTAINER_FORMAT)

    def test_unverified_python_version_is_refused(self):
        """A version nobody proved is refused even though it is newer."""
        document = contract()
        document["target"]["python_versions"] = ["3.12.13", "3.13.14", "3.14.0"]
        self.assertRefused(
            document,
            Host("3.14.0", "Linux", "x86_64"),
            abi.REASON_PYTHON_NOT_VERIFIED_BY_RUNTIME,
        )

    def test_an_image_cannot_widen_the_runtime_verified_set(self):
        """The image's allowlist never overrides what the runtime has verified."""
        document = contract()
        document["target"]["python_versions"] = ["3.11.0"]
        document["creator"]["python_version"] = "3.11.0"
        self.assertRefused(
            document,
            Host("3.11.0", "Linux", "x86_64"),
            abi.REASON_PYTHON_NOT_VERIFIED_BY_RUNTIME,
        )

    def test_python_outside_the_image_allowlist_is_refused(self):
        document = contract()
        document["target"]["python_versions"] = ["3.12.13"]
        self.assertRefused(
            document, MACOS_313, abi.REASON_PYTHON_NOT_IN_IMAGE_ALLOWLIST
        )

    def test_missing_required_capability_is_refused(self):
        document = contract()
        document["target"]["required_capabilities"] = sorted(
            set(document["target"]["required_capabilities"]) | {"time-travel-1.0"}
        )
        exception = self.assertRefused(
            document, LINUX_312, abi.REASON_MISSING_CAPABILITY
        )
        self.assertIn("time-travel-1.0", str(exception))

    def test_omitting_a_mandatory_capability_is_refused(self):
        document = contract()
        document["target"]["required_capabilities"] = [
            item
            for item in document["target"]["required_capabilities"]
            if item != "explicit-frames"
        ]
        self.assertRefused(document, LINUX_312, abi.REASON_UNKNOWN_CAPABILITY)

    def test_unknown_runtime_implementation_is_refused(self):
        document = contract()
        document["target"]["runtime_implementations"] = ["someone-elses-vm"]
        self.assertRefused(
            document, LINUX_312, abi.REASON_UNKNOWN_RUNTIME_IMPLEMENTATION
        )

    def test_native_payload_requirement_is_refused(self):
        document = contract()
        document["target"]["native_payload_required"] = True
        self.assertRefused(document, LINUX_312, abi.REASON_NATIVE_PAYLOAD_REQUIRED)

    def test_policy_downgrade_is_refused(self):
        """A contract image may not ask for the weaker legacy rule."""
        document = contract()
        document["compatibility_policy"] = abi.POLICY_EXACT
        self.assertRefused(document, LINUX_312, abi.REASON_POLICY_DOWNGRADE)

    def test_unknown_policy_is_refused(self):
        document = contract()
        document["compatibility_policy"] = "trust-me"
        self.assertRefused(document, LINUX_312, abi.REASON_MALFORMED_CONTRACT)

    def test_unsupported_operating_system_is_refused(self):
        document = contract()
        document["target"]["operating_systems"] = ["Linux"]
        self.assertRefused(
            document, MACOS_313, abi.REASON_UNSUPPORTED_OPERATING_SYSTEM
        )

    def test_unsupported_architecture_is_refused(self):
        document = contract()
        document["target"]["architectures"] = ["x86_64"]
        self.assertRefused(document, MACOS_313, abi.REASON_UNSUPPORTED_ARCHITECTURE)

    def test_unsupported_platform_pair_is_refused(self):
        """OS and ISA may each be listed while the pair is still not verified."""
        document = contract()
        document["target"]["platforms"] = [
            entry
            for entry in document["target"]["platforms"]
            if entry != {"os": "Darwin", "architecture": "arm64"}
        ]
        self.assertRefused(document, MACOS_313, abi.REASON_UNSUPPORTED_PLATFORM)

    def test_inconsistent_creator_provenance_is_refused(self):
        document = contract()
        document["creator"]["python_version"] = "3.9.1"
        self.assertRefused(document, LINUX_312, abi.REASON_INCONSISTENT_PROVENANCE)


class MalformedAllowlistTests(unittest.TestCase):
    """Bounded parsing: an ambiguous allowlist is refused, never guessed at."""

    def assertMalformedAllowlist(self, value):
        document = contract()
        document["target"]["python_versions"] = value
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(document, LINUX_312)
        self.assertEqual(
            caught.exception.reason, abi.REASON_MALFORMED_PYTHON_ALLOWLIST
        )

    def test_empty_allowlist(self):
        self.assertMalformedAllowlist([])

    def test_duplicated_allowlist_entries(self):
        self.assertMalformedAllowlist(["3.12.13", "3.12.13"])

    def test_non_string_allowlist_entries(self):
        self.assertMalformedAllowlist(["3.12.13", 313])

    def test_allowlist_is_not_a_list(self):
        self.assertMalformedAllowlist("3.12.13")

    def test_allowlist_with_null_entry(self):
        self.assertMalformedAllowlist(["3.12.13", None])

    def test_oversized_allowlist(self):
        self.assertMalformedAllowlist(
            [f"3.12.{index}" for index in range(abi.MAX_LIST_ENTRIES + 1)]
        )

    def test_oversized_allowlist_string(self):
        self.assertMalformedAllowlist(["3.12.13", "9" * (abi.MAX_STRING_LENGTH + 1)])


class MalformedContractTests(unittest.TestCase):
    def assertMalformed(self, mutate, reason=abi.REASON_MALFORMED_CONTRACT):
        document = contract()
        mutate(document)
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(document, LINUX_312)
        self.assertEqual(caught.exception.reason, reason)

    def test_contract_is_not_an_object(self):
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(["not", "a", "contract"], LINUX_312)
        self.assertEqual(caught.exception.reason, abi.REASON_MALFORMED_CONTRACT)

    def test_contract_is_none(self):
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(None, LINUX_312)
        self.assertEqual(caught.exception.reason, abi.REASON_MALFORMED_CONTRACT)

    def test_missing_creator_section(self):
        self.assertMalformed(lambda document: document.pop("creator"))

    def test_creator_is_not_an_object(self):
        self.assertMalformed(lambda document: document.update(creator="linux"))

    def test_missing_target_section(self):
        self.assertMalformed(lambda document: document.pop("target"))

    def test_missing_execution_abi_field(self):
        self.assertMalformed(lambda document: document.pop("execution_abi_version"))

    def test_malformed_platform_entry(self):
        self.assertMalformed(
            lambda document: document["target"].update(platforms=[{"os": "Linux"}])
        )

    def test_platform_entry_with_extra_keys(self):
        self.assertMalformed(
            lambda document: document["target"].update(
                platforms=[{"os": "Linux", "architecture": "x86_64", "extra": 1}]
            )
        )

    def test_empty_capability_list(self):
        self.assertMalformed(
            lambda document: document["target"].update(required_capabilities=[])
        )


class LegacyContractTests(unittest.TestCase):
    """Format 0.1 keeps its original strict rule, with a versioned message."""

    def legacy(self):
        return {"python_version": "3.12.13", "runtime_version": "0.3.1"}

    def test_matching_legacy_host_is_accepted(self):
        abi.legacy_decision(
            self.legacy(), Host("3.12.13", "Linux", "x86_64", continuum_version="0.3.1")
        )

    def test_legacy_image_refuses_a_different_python(self):
        with self.assertRaises(IncompatibleImage) as caught:
            abi.legacy_decision(
                self.legacy(),
                Host("3.13.14", "Linux", "x86_64", continuum_version="0.3.1"),
            )
        self.assertEqual(caught.exception.reason, abi.REASON_LEGACY_PYTHON_MISMATCH)
        # The message must explain why the stricter rule applied and what to do.
        self.assertIn(abi.LEGACY_CONTAINER_FORMAT_VERSION, str(caught.exception))
        self.assertIn(abi.CONTAINER_FORMAT_VERSION, str(caught.exception))

    def test_legacy_image_refuses_a_different_runtime_version(self):
        with self.assertRaises(IncompatibleImage) as caught:
            abi.legacy_decision(
                self.legacy(),
                Host("3.12.13", "Linux", "x86_64", continuum_version="0.4.0a1"),
            )
        self.assertEqual(caught.exception.reason, abi.REASON_LEGACY_RUNTIME_MISMATCH)
        self.assertIn(abi.CONTAINER_FORMAT_VERSION, str(caught.exception))

    def test_legacy_image_cannot_reach_a_platform_the_runtime_refuses(self):
        """The older format is a reason to be stricter, never a way around the
        runtime side of the platform decision.

        The 0.2 path refuses Windows arm64 on this runtime's own evidence, not
        on anything the image declares. A 0.1 image carries no contract to
        argue with, so if the container format were the only difference an
        image could reach an unverified pair simply by being older -- with
        every archive checksum recomputed to match.
        """

        pair = ("Windows", "arm64")
        self.assertNotIn(pair, abi.VERIFIED_PLATFORMS)
        with self.assertRaises(IncompatibleImage) as caught:
            abi.legacy_decision(
                self.legacy(), Host("3.12.13", *pair, continuum_version="0.3.1")
            )
        self.assertEqual(caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM)

    def test_the_legacy_platform_gate_runs_before_the_exact_checks(self):
        """An unverified pair is refused as such, not as a version mismatch.

        Reporting "wrong Python" on a host that would be refused whatever its
        Python is sends the reader off to re-freeze an image that can never
        restore there.
        """

        with self.assertRaises(IncompatibleImage) as caught:
            abi.legacy_decision(
                self.legacy(),
                Host("3.13.14", "Windows", "arm64", continuum_version="9.9.9"),
            )
        self.assertEqual(caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM)


class GraphCodecVersionTests(unittest.TestCase):
    """One version, declared once: the bytes the codec writes and the
    capability the execution contract advertises cannot drift apart."""

    def test_the_encoder_stamps_the_abi_constant(self):
        from continuum.codec import GraphEncoder

        self.assertEqual(
            GraphEncoder().encode(1)["codec_version"], abi.GRAPH_CODEC_VERSION
        )

    def test_the_decoder_accepts_exactly_the_abi_constant(self):
        from continuum.codec import GraphDecoder
        from continuum.errors import ImageError

        with self.assertRaises(ImageError):
            GraphDecoder(
                {
                    "codec_version": abi.GRAPH_CODEC_VERSION + "-not-this",
                    "root": None,
                    "objects": [],
                }
            )


class ContractShapeTests(unittest.TestCase):
    def test_mandatory_capabilities_are_all_provided_by_this_runtime(self):
        self.assertLessEqual(abi.MANDATORY_CAPABILITIES, abi.PROVIDED_CAPABILITIES)

    def test_capability_names_carry_their_versions(self):
        self.assertIn(f"continuum-ir-{IR_VERSION}", abi.PROVIDED_CAPABILITIES)
        self.assertIn(
            f"graph-codec-{abi.GRAPH_CODEC_VERSION}", abi.PROVIDED_CAPABILITIES
        )
        self.assertIn(
            f"execution-abi-{abi.EXECUTION_ABI_VERSION}", abi.PROVIDED_CAPABILITIES
        )

    def test_verified_versions_are_exact_and_never_ranges(self):
        for version in abi.VERIFIED_PYTHON_VERSIONS:
            with self.subTest(version=version):
                self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_the_shipping_exact_python_remains_verified(self):
        """Cross-Python support must not drop the version main already shipped."""
        self.assertIn(abi.SUPPORTED_PYTHON, abi.VERIFIED_PYTHON_VERSIONS)

    def test_parse_contract_does_not_mutate_its_input(self):
        document = contract()
        before = copy.deepcopy(document)
        abi.parse_contract(document)
        self.assertEqual(document, before)

    def test_decide_restore_returns_a_detached_contract(self):
        document = contract()
        accepted = abi.decide_restore(document, LINUX_312)
        accepted["target"]["python_versions"].append("9.9.9")
        self.assertNotIn("9.9.9", document["target"]["python_versions"])




class PlatformDoubleGateTests(unittest.TestCase):
    """The platform pair is decided by the image and the runtime, not either alone.

    Before this gate existed the pair was checked only against the image's own
    lists, so an image that named Windows arm64 was accepted on a Windows arm64
    host even though this runtime never accepted that pair. The untrusted
    document decided its own admissibility.
    """

    def widened(self) -> dict:
        """A contract that claims Windows arm64 for itself."""
        document = contract()
        target = document["target"]
        target["operating_systems"] = ["Linux", "Darwin", "Windows"]
        target["architectures"] = ["x86_64", "arm64"]
        target["platforms"] = target["platforms"] + [
            {"os": "Windows", "architecture": "arm64"}
        ]
        return document

    def test_the_runtime_refuses_a_pair_it_does_not_accept(self):
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(self.widened(), Host("3.12.13", "Windows", "arm64"))
        self.assertEqual(caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM)
        self.assertIn("this runtime does not accept platform", str(caught.exception))

    def test_the_image_still_refuses_a_pair_it_does_not_list(self):
        """The image-side half of the gate must remain in force."""
        document = contract()
        document["target"]["platforms"] = [
            entry
            for entry in document["target"]["platforms"]
            if entry != {"os": "Darwin", "architecture": "arm64"}
        ]
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(document, MACOS_313)
        self.assertEqual(caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM)
        self.assertIn("image does not accept platform", str(caught.exception))

    def test_a_restricted_runtime_refuses_a_pair_the_image_allows(self):
        """Narrowing the runtime's own list is enough to refuse, by itself."""
        host = Host(
            "3.12.13", "Darwin", "arm64", verified_platforms=(("Linux", "x86_64"),)
        )
        with self.assertRaises(IncompatibleImage) as caught:
            abi.decide_restore(contract(), host)
        self.assertEqual(caught.exception.reason, abi.REASON_UNSUPPORTED_PLATFORM)

    def test_every_runtime_accepted_pair_is_still_accepted(self):
        for name, machine in abi.VERIFIED_PLATFORMS:
            with self.subTest(platform=f"{name} {machine}"):
                abi.decide_restore(contract(), Host("3.12.13", name, machine))

    def test_the_runtime_list_matches_the_declared_target_platforms(self):
        self.assertEqual(
            sorted(abi.VERIFIED_PLATFORMS),
            sorted(
                (entry["os"], entry["architecture"]) for entry in abi.TARGET_PLATFORMS
            ),
        )

    def test_windows_arm64_is_absent_from_the_runtime_list(self):
        """The pair the project has always documented as unsupported."""
        self.assertNotIn(("Windows", "arm64"), abi.VERIFIED_PLATFORMS)


if __name__ == "__main__":
    unittest.main()
