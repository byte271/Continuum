"""The versioned execution compatibility contract.

Continuum executes its own IR in its own virtual machine. Live execution state
lives in Continuum's `Frame` objects — logical program counters, operand stacks,
lexical cells, and control blocks — never in CPython frame objects. That is the
whole reason an image can outlive the interpreter that produced it.

The shipping 0.1 container inherited a stricter rule than that design requires:
it demanded the exact creator Python version *and* the exact creator Continuum
version before restoring. Both facts are provenance. Neither is what actually
determines whether a target can reconstruct the state.

This module separates the axes that were previously collapsed into those two
fields, so a restore decision can be made from the properties that matter:

======================  ===============================================
container format        how the archive is laid out
graph codec             how the object graph is encoded
Continuum IR            the instruction set the frames refer to
execution ABI           the meaning of frame/binding/stack state
creator runtime         provenance: which Continuum wrote the image
creator Python          provenance: which interpreter wrote the image
target runtimes         which runtime implementations may restore it
target Python versions  which interpreters are explicitly verified
required capabilities   named features the target must implement
======================  ===============================================

A target may restore an image only when it implements the exact execution ABI
and every required capability, and only when the running interpreter appears in
both the image's allowlist and this runtime's independently verified list. An
image cannot widen that set by asserting a version this runtime has never
verified, and an unverified interpreter is refused rather than attempted.

Every refusal carries a stable machine-readable reason code so that policy is
testable without string matching.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any, Mapping

from . import FORMAT_VERSION, IR_VERSION, SUPPORTED_PYTHON, __version__
from .errors import ImageError

# The container layout that carries an explicit execution contract. Format 0.1
# images predate the contract and are read under the legacy rule below.
CONTAINER_FORMAT_VERSION = "0.2"
LEGACY_CONTAINER_FORMAT_VERSION = FORMAT_VERSION

# The object-graph encoding, versioned independently of the container. Bump this
# whenever an encoded graph from an older writer would decode to a different
# object shape.
GRAPH_CODEC_VERSION = "0.1"

# The meaning of serialized execution state: what a frame's logical program
# counter indexes, how operand stacks and lexical cells are laid out, and how
# control blocks and pending finally reasons are represented. Bump this whenever
# an older image's frames would be misinterpreted by this runtime.
EXECUTION_ABI_VERSION = "1.0"

RUNTIME_IMPLEMENTATION = "continuum-vm"
SUPPORTED_RUNTIME_IMPLEMENTATIONS = ("continuum-vm",)

# Interpreters on which this runtime's execution ABI is verified end to end by
# native CI: freeze on one, restore on another, compare against an independent
# uninterrupted control. Adding an entry requires a green cross-Python proof
# run; it is never a guess and never a range.
VERIFIED_PYTHON_VERSIONS = ("3.12.13", "3.13.14")

# Named features a target must implement to restore an image. The IR, graph
# codec, and execution ABI appear here as versioned capabilities so that an
# image which needs a newer one is refused by name rather than by accident.
PROVIDED_CAPABILITIES = frozenset(
    {
        f"continuum-ir-{IR_VERSION}",
        f"graph-codec-{GRAPH_CODEC_VERSION}",
        f"execution-abi-{EXECUTION_ABI_VERSION}",
        "explicit-frames",
        "portable-readonly-files",
    }
)

# Capabilities every contract image must require. A contract that omits one of
# these is not describing state this runtime knows how to reconstruct.
MANDATORY_CAPABILITIES = frozenset(
    {
        f"continuum-ir-{IR_VERSION}",
        f"graph-codec-{GRAPH_CODEC_VERSION}",
        f"execution-abi-{EXECUTION_ABI_VERSION}",
        "explicit-frames",
    }
)

TARGET_OPERATING_SYSTEMS = ("Linux", "Darwin", "Windows")
TARGET_ARCHITECTURES = ("x86_64", "arm64")
TARGET_PLATFORMS = (
    {"os": "Linux", "architecture": "x86_64"},
    {"os": "Linux", "architecture": "arm64"},
    {"os": "Darwin", "architecture": "x86_64"},
    {"os": "Darwin", "architecture": "arm64"},
    {"os": "Windows", "architecture": "x86_64"},
)

# Bounds for parsing untrusted contract documents. An image is executable
# untrusted content, so every list it declares is length-bounded and every
# string it declares is size-bounded before the values are used.
MAX_LIST_ENTRIES = 64
MAX_STRING_LENGTH = 128

# Stable refusal reason codes. Tests assert on these rather than on prose.
REASON_MALFORMED_CONTRACT = "malformed-contract"
REASON_UNKNOWN_CONTAINER_FORMAT = "unknown-container-format"
REASON_UNKNOWN_GRAPH_CODEC = "unknown-graph-codec"
REASON_UNKNOWN_IR_VERSION = "unknown-ir-version"
REASON_UNKNOWN_EXECUTION_ABI = "unknown-execution-abi"
REASON_UNKNOWN_RUNTIME_IMPLEMENTATION = "unknown-runtime-implementation"
REASON_NATIVE_PAYLOAD_REQUIRED = "native-payload-required"
REASON_MISSING_CAPABILITY = "missing-capability"
REASON_UNKNOWN_CAPABILITY = "unknown-capability"
REASON_MALFORMED_PYTHON_ALLOWLIST = "malformed-python-allowlist"
REASON_PYTHON_NOT_IN_IMAGE_ALLOWLIST = "python-not-in-image-allowlist"
REASON_PYTHON_NOT_VERIFIED_BY_RUNTIME = "python-not-verified-by-runtime"
REASON_UNSUPPORTED_OPERATING_SYSTEM = "unsupported-operating-system"
REASON_UNSUPPORTED_ARCHITECTURE = "unsupported-architecture"
REASON_UNSUPPORTED_PLATFORM = "unsupported-platform"
REASON_INCONSISTENT_PROVENANCE = "inconsistent-provenance"
REASON_POLICY_DOWNGRADE = "policy-downgrade"
REASON_LEGACY_PYTHON_MISMATCH = "legacy-python-mismatch"
REASON_LEGACY_RUNTIME_MISMATCH = "legacy-runtime-mismatch"

# The compatibility policy an image asks the target to apply. `execution-abi`
# is the contract policy defined here. `exact` is the legacy 0.1 rule. An image
# may not claim a policy weaker than the one its container format defines.
POLICY_EXECUTION_ABI = "execution-abi"
POLICY_EXACT = "exact"
KNOWN_POLICIES = (POLICY_EXECUTION_ABI, POLICY_EXACT)


class IncompatibleImage(ImageError):
    """A restore was refused before any execution state was reconstructed.

    Carries a stable `reason` code alongside the human-readable message so
    policy tests never depend on prose.
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{detail} [{reason}]")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Host:
    """The identity a restore decision is made against.

    Constructed from the live interpreter in production and by hand in tests, so
    every refusal path is reachable deterministically on a single interpreter.
    """

    python_version: str
    operating_system: str
    architecture: str
    runtime_implementation: str = RUNTIME_IMPLEMENTATION
    continuum_version: str = __version__
    provided_capabilities: frozenset[str] = PROVIDED_CAPABILITIES
    verified_python_versions: tuple[str, ...] = VERIFIED_PYTHON_VERSIONS
    execution_abi_version: str = EXECUTION_ABI_VERSION
    graph_codec_version: str = GRAPH_CODEC_VERSION
    ir_version: str = IR_VERSION


def normalized_architecture(value: str | None = None) -> str:
    machine = (value if value is not None else platform.machine()).lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        machine, machine
    )


def current_host() -> Host:
    return Host(
        python_version=platform.python_version(),
        operating_system=platform.system(),
        architecture=normalized_architecture(),
    )


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH:
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, f"contract field {field!r} is not a valid string"
        )
    return value


def _require_string_list(value: Any, field: str) -> list[str]:
    """Parse an untrusted allowlist under explicit bounds.

    Empty lists, duplicates, non-strings, and oversized lists are all malformed
    rather than merely unsatisfiable: a target must never have to guess what an
    ambiguous allowlist meant.
    """
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_LIST_ENTRIES
        or any(
            not isinstance(item, str) or not item or len(item) > MAX_STRING_LENGTH
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, f"contract field {field!r} is not a valid list"
        )
    return list(value)


def build_contract(
    creator_os: str,
    creator_architecture: str,
    creator_python: str,
    creator_continuum_version: str = __version__,
) -> dict[str, Any]:
    """Build the execution contract a freshly written image declares.

    Creator identity is recorded as provenance. The target decision is driven by
    the execution ABI, the capability list, and the verified Python allowlist.
    """

    return {
        "container_format_version": CONTAINER_FORMAT_VERSION,
        "graph_codec_version": GRAPH_CODEC_VERSION,
        "ir_version": IR_VERSION,
        "execution_abi_version": EXECUTION_ABI_VERSION,
        "compatibility_policy": POLICY_EXECUTION_ABI,
        "creator": {
            "continuum_version": creator_continuum_version,
            "python_version": creator_python,
            "os": creator_os,
            "architecture": creator_architecture,
        },
        "target": {
            "runtime_implementations": list(SUPPORTED_RUNTIME_IMPLEMENTATIONS),
            "python_versions": list(VERIFIED_PYTHON_VERSIONS),
            "operating_systems": list(TARGET_OPERATING_SYSTEMS),
            "architectures": list(TARGET_ARCHITECTURES),
            "platforms": [dict(entry) for entry in TARGET_PLATFORMS],
            "required_capabilities": sorted(MANDATORY_CAPABILITIES),
            "native_payload_required": False,
        },
    }


def parse_contract(document: Any) -> dict[str, Any]:
    """Structurally validate an untrusted contract document.

    Runs before any restore decision and before any execution state is touched,
    so a malformed contract is refused rather than partially interpreted.
    """

    if not isinstance(document, Mapping):
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, "execution contract is not an object"
        )

    container = _require_string(
        document.get("container_format_version"), "container_format_version"
    )
    codec = _require_string(document.get("graph_codec_version"), "graph_codec_version")
    ir_version = _require_string(document.get("ir_version"), "ir_version")
    abi = _require_string(
        document.get("execution_abi_version"), "execution_abi_version"
    )
    policy = _require_string(document.get("compatibility_policy"), "compatibility_policy")
    if policy not in KNOWN_POLICIES:
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, f"unknown compatibility policy {policy!r}"
        )

    creator = document.get("creator")
    if not isinstance(creator, Mapping):
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, "contract creator provenance is not an object"
        )
    creator_parsed = {
        "continuum_version": _require_string(
            creator.get("continuum_version"), "creator.continuum_version"
        ),
        "python_version": _require_string(
            creator.get("python_version"), "creator.python_version"
        ),
        "os": _require_string(creator.get("os"), "creator.os"),
        "architecture": _require_string(
            creator.get("architecture"), "creator.architecture"
        ),
    }

    target = document.get("target")
    if not isinstance(target, Mapping):
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, "contract target section is not an object"
        )
    if target.get("native_payload_required") is not False:
        raise IncompatibleImage(
            REASON_NATIVE_PAYLOAD_REQUIRED,
            "image requires a native payload this runtime cannot provide",
        )

    platforms = target.get("platforms")
    if (
        not isinstance(platforms, list)
        or not platforms
        or len(platforms) > MAX_LIST_ENTRIES
        or any(
            not isinstance(entry, Mapping)
            or set(entry) != {"os", "architecture"}
            or not isinstance(entry["os"], str)
            or not isinstance(entry["architecture"], str)
            for entry in platforms
        )
    ):
        raise IncompatibleImage(
            REASON_MALFORMED_CONTRACT, "contract target platform list is malformed"
        )

    try:
        python_versions = _require_string_list(
            target.get("python_versions"), "target.python_versions"
        )
    except IncompatibleImage as exc:
        # A malformed Python allowlist gets its own reason code: it is the field
        # most likely to be attacked, and callers test it specifically.
        raise IncompatibleImage(
            REASON_MALFORMED_PYTHON_ALLOWLIST,
            "contract target Python allowlist is malformed",
        ) from exc

    return {
        "container_format_version": container,
        "graph_codec_version": codec,
        "ir_version": ir_version,
        "execution_abi_version": abi,
        "compatibility_policy": policy,
        "creator": creator_parsed,
        "target": {
            "runtime_implementations": _require_string_list(
                target.get("runtime_implementations"), "target.runtime_implementations"
            ),
            "python_versions": python_versions,
            "operating_systems": _require_string_list(
                target.get("operating_systems"), "target.operating_systems"
            ),
            "architectures": _require_string_list(
                target.get("architectures"), "target.architectures"
            ),
            "platforms": [dict(entry) for entry in platforms],
            "required_capabilities": _require_string_list(
                target.get("required_capabilities"), "target.required_capabilities"
            ),
            "native_payload_required": False,
        },
    }


def decide_restore(document: Any, host: Host) -> dict[str, Any]:
    """Decide whether `host` may restore an image declaring `document`.

    Returns the parsed contract on acceptance and raises `IncompatibleImage`
    with a stable reason code on refusal. Pure: it reads no global interpreter
    state, so every branch is reachable in a test on a single interpreter.
    """

    contract = parse_contract(document)

    if contract["container_format_version"] != CONTAINER_FORMAT_VERSION:
        raise IncompatibleImage(
            REASON_UNKNOWN_CONTAINER_FORMAT,
            f"image container format {contract['container_format_version']!r} is not "
            f"supported; this runtime implements {CONTAINER_FORMAT_VERSION!r}",
        )
    # The contract policy is defined by the container format. An image that
    # carries a 0.2 contract but asks for the weaker legacy rule is trying to
    # downgrade the policy, which is refused rather than honoured.
    if contract["compatibility_policy"] != POLICY_EXECUTION_ABI:
        raise IncompatibleImage(
            REASON_POLICY_DOWNGRADE,
            f"container format {CONTAINER_FORMAT_VERSION} requires the "
            f"{POLICY_EXECUTION_ABI!r} policy, image declares "
            f"{contract['compatibility_policy']!r}",
        )
    if contract["graph_codec_version"] != host.graph_codec_version:
        raise IncompatibleImage(
            REASON_UNKNOWN_GRAPH_CODEC,
            f"image graph codec {contract['graph_codec_version']!r} is not supported; "
            f"this runtime implements {host.graph_codec_version!r}",
        )
    if contract["ir_version"] != host.ir_version:
        raise IncompatibleImage(
            REASON_UNKNOWN_IR_VERSION,
            f"image IR version {contract['ir_version']!r} is not supported; this "
            f"runtime implements {host.ir_version!r}",
        )
    if contract["execution_abi_version"] != host.execution_abi_version:
        raise IncompatibleImage(
            REASON_UNKNOWN_EXECUTION_ABI,
            f"image execution ABI {contract['execution_abi_version']!r} is not "
            f"supported; this runtime implements {host.execution_abi_version!r}",
        )

    target = contract["target"]
    if host.runtime_implementation not in target["runtime_implementations"]:
        raise IncompatibleImage(
            REASON_UNKNOWN_RUNTIME_IMPLEMENTATION,
            f"image does not accept runtime implementation "
            f"{host.runtime_implementation!r}",
        )

    required = set(target["required_capabilities"])
    unknown = sorted(required - host.provided_capabilities)
    if unknown:
        raise IncompatibleImage(
            REASON_MISSING_CAPABILITY,
            f"this runtime does not implement required capabilities: {unknown}",
        )
    absent = sorted(MANDATORY_CAPABILITIES - required)
    if absent:
        raise IncompatibleImage(
            REASON_UNKNOWN_CAPABILITY,
            f"image omits mandatory execution capabilities: {absent}",
        )

    if host.operating_system not in target["operating_systems"]:
        raise IncompatibleImage(
            REASON_UNSUPPORTED_OPERATING_SYSTEM,
            f"image does not accept operating system {host.operating_system!r}",
        )
    if host.architecture not in target["architectures"]:
        raise IncompatibleImage(
            REASON_UNSUPPORTED_ARCHITECTURE,
            f"image does not accept architecture {host.architecture!r}",
        )
    if {
        "os": host.operating_system,
        "architecture": host.architecture,
    } not in target["platforms"]:
        raise IncompatibleImage(
            REASON_UNSUPPORTED_PLATFORM,
            f"image does not accept platform {host.operating_system} "
            f"{host.architecture}",
        )

    # Two independent gates. The image says which interpreters its creator was
    # willing to target; this runtime says which interpreters it has actually
    # verified. An image cannot widen the second set by asserting the first.
    if host.python_version not in target["python_versions"]:
        raise IncompatibleImage(
            REASON_PYTHON_NOT_IN_IMAGE_ALLOWLIST,
            f"image does not accept Python {host.python_version}; it accepts "
            f"{target['python_versions']}",
        )
    if host.python_version not in host.verified_python_versions:
        raise IncompatibleImage(
            REASON_PYTHON_NOT_VERIFIED_BY_RUNTIME,
            f"this runtime has not verified Python {host.python_version}; verified "
            f"versions are {list(host.verified_python_versions)}",
        )

    # Creator provenance must be internally coherent even though it does not
    # gate the restore: an image whose creator Python is absent from its own
    # target allowlist is describing a state no target was ever meant to accept.
    if contract["creator"]["python_version"] not in target["python_versions"]:
        raise IncompatibleImage(
            REASON_INCONSISTENT_PROVENANCE,
            "creator Python version is absent from the image's own target allowlist",
        )

    return contract


def legacy_decision(
    compatibility: Mapping[str, Any], host: Host
) -> None:
    """Apply the format 0.1 rule: exact creator Python and exact runtime.

    Format 0.1 images carry no execution contract, so there is nothing to make a
    capability-based decision from. Rather than guessing that such an image is
    ABI-compatible, this keeps the original strict rule and reports refusals
    with the format version named, so the message explains *why* the stricter
    rule applied.
    """

    image_python = compatibility.get("python_version")
    if image_python != host.python_version:
        raise IncompatibleImage(
            REASON_LEGACY_PYTHON_MISMATCH,
            f"container format {LEGACY_CONTAINER_FORMAT_VERSION} images require the "
            f"exact creator Python {image_python!r}; this runtime is "
            f"{host.python_version!r}. Re-freeze under container format "
            f"{CONTAINER_FORMAT_VERSION} for cross-Python restore",
        )
    if compatibility.get("runtime_version") != host.continuum_version:
        raise IncompatibleImage(
            REASON_LEGACY_RUNTIME_MISMATCH,
            f"container format {LEGACY_CONTAINER_FORMAT_VERSION} images require the "
            f"exact creator runtime {compatibility.get('runtime_version')!r}; this "
            f"runtime is {host.continuum_version!r}. Re-freeze under container "
            f"format {CONTAINER_FORMAT_VERSION} for runtime-version independence",
        )


def contract_summary(contract: Mapping[str, Any]) -> dict[str, Any]:
    """A flat, human-readable view of an accepted contract for `inspect`."""

    target = contract["target"]
    creator = contract["creator"]
    return {
        "container_format_version": contract["container_format_version"],
        "graph_codec_version": contract["graph_codec_version"],
        "ir_version": contract["ir_version"],
        "execution_abi_version": contract["execution_abi_version"],
        "compatibility_policy": contract["compatibility_policy"],
        "creator_continuum_version": creator["continuum_version"],
        "creator_python_version": creator["python_version"],
        "creator_platform": f"{creator['os']} {creator['architecture']}",
        "target_python_versions": list(target["python_versions"]),
        "target_runtime_implementations": list(target["runtime_implementations"]),
        "required_capabilities": list(target["required_capabilities"]),
    }


__all__ = [
    "CONTAINER_FORMAT_VERSION",
    "EXECUTION_ABI_VERSION",
    "GRAPH_CODEC_VERSION",
    "Host",
    "IncompatibleImage",
    "LEGACY_CONTAINER_FORMAT_VERSION",
    "MANDATORY_CAPABILITIES",
    "PROVIDED_CAPABILITIES",
    "SUPPORTED_PYTHON",
    "VERIFIED_PYTHON_VERSIONS",
    "build_contract",
    "contract_summary",
    "current_host",
    "decide_restore",
    "legacy_decision",
    "normalized_architecture",
    "parse_contract",
]
