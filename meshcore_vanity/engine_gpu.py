"""Native prefix-pattern harvesting and legacy exact-prefix campaigns.

Current binaries stream prefix-pattern results and exact progress statistics.
Python supplies conservative screening thresholds and independently verifies
and ranks every accepted key. Older binaries, or --prefix-campaigns, use the
bounded exact-prefix scheduler below.

Two behaviours worth knowing about:

*Escalation.* Once every affordable target has been found, an earlier design
idled forever. Here the difficulty budget is multiplied instead, so a machine
left running keeps reaching for longer and rarer prefixes rather than going to
sleep with the GPU idle.

*Capability detection.* A binary built without a GPU feature rejects the
``--gpu-only`` flag outright. The backend is probed once at startup and the
command line adapts, so a CPU-only build still contributes instead of failing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import STATE_FORMAT_VERSION, project_revision
from .catalog import HEX_DIGITS, HEX_WORDS, LOCAL_EXACT_PREFIXES
from .config import Config, GpuConfig, PROJECT_DIRECTORY
from .harvest import build_policy
from .keys import (
    is_reserved_public_key,
    loose_hex,
    meshcore_private_key_from_seed,
    public_key_from_meshcore_private,
    public_key_from_seed,
)
from .scoring import analyze_public_key
from .storage import add_integrity_hash, atomic_write_json, read_verified_json, utc_now

GpuTarget = Tuple[str, int, str]


# --- Target catalog --------------------------------------------------------


def build_targets(config: GpuConfig) -> Tuple[GpuTarget, ...]:
    """Turn the pattern catalog into prioritised exact prefixes."""
    targets: Dict[str, Tuple[int, str]] = {}
    low, high = config.min_target_length, config.max_target_length

    def add(prefix: str, priority: int, description: str) -> None:
        prefix = prefix.upper()
        if not low <= len(prefix) <= high:
            return
        if any(c not in HEX_DIGITS for c in prefix) or prefix.startswith(("00", "FF")):
            return
        existing = targets.get(prefix)
        if existing is None or priority > existing[0]:
            targets[prefix] = (priority, description)

    def add_repetitions(unit: str, base: int, step: int, description: str) -> None:
        if not unit:
            return
        repeated = unit * ((high // len(unit)) + 2)
        for length in range(low, high + 1):
            add(repeated[:length], base + length * step, description)

    for prefix, (quality, description) in LOCAL_EXACT_PREFIXES.items():
        add(prefix, 680_000 + quality + len(prefix) * 24_000, description)
        add_repetitions(prefix, 650_000 + quality, 28_000, f"repeated local prefix '{prefix}'")

    add_repetitions("514", 720_000, 32_000, "Montreal 514 repetition")
    add_repetitions("4", 760_000, 32_000, "preferred all-4 run")

    for word, (quality, description) in HEX_WORDS.items():
        add(word, 620_000 + quality + len(word) * 24_000, description)
        if len(word) >= 4:
            for length in range(max(low, len(word) + 1), high + 1):
                add(word + word[-1] * (length - len(word)),
                    560_000 + quality + length * 24_000,
                    f"word '{word}' with extended final run")
        if len(word) >= 3:
            add_repetitions(word, 650_000 + quality, 30_000, f"repeated word '{word}'")

    strongest = [
        word for word, _entry in sorted(
            HEX_WORDS.items(), key=lambda item: (-item[1][0], -len(item[0]), item[0])
        )[:24]
    ]
    for left in strongest:
        for right in strongest:
            compound = left + right
            add(compound,
                620_000 + HEX_WORDS[left][0] + HEX_WORDS[right][0] + len(compound) * 24_000,
                f"compound words '{left}' + '{right}'")
            for length in range(max(low, len(compound) + 1), high + 1):
                add(compound + compound[-1] * (length - len(compound)),
                    560_000 + HEX_WORDS[left][0] + HEX_WORDS[right][0] + length * 24_000,
                    f"compound words '{left}' + '{right}' with extended final run")

    for character in "123456789ABCDE":
        for length in range(low, high + 1):
            add(character * length,
                750_000 + length * 35_000 + (80_000 if character == "4" else 0),
                f"{length} identical leading '{character}' characters")

    for sequence, label in (("0123456789ABCDEF", "ascending"), ("FEDCBA9876543210", "descending")):
        for start in range(len(sequence) - low + 1):
            for length in range(low, min(high, len(sequence) - start) + 1):
                add(sequence[start:start + length], 640_000 + length * 26_000,
                    f"{label} sequence starting at '{sequence[start]}'")

    for unit in ("14", "44", "51", "514", "AB", "BA", "ACE", "CA", "FE", "DE",
                 "AD", "BE", "EE", "C0", "C0FFEE", "CAFE", "BEBE", "FACE", "DEAD"):
        add_repetitions(unit, 600_000, 26_000, f"periodic prefix from unit '{unit}'")

    return tuple(
        (prefix, priority, description)
        for prefix, (priority, description) in sorted(
            targets.items(), key=lambda item: (len(item[0]), -item[1][0], item[0])
        )
    )


_EXPECTED_SCORE_CACHE: Dict[str, int] = {}


def expected_target_score(prefix: str) -> int:
    """What a key starting with this prefix would score, filler aside.

    A heuristic for target selection, not an upper bound: a random suffix can
    always extend the prefix or contain another, better pattern.
    """
    cached = _EXPECTED_SCORE_CACHE.get(prefix)
    if cached is not None:
        return cached
    filler = hashlib.sha256(f"{prefix}|target-estimate".encode("ascii")).hexdigest().upper()
    candidate = (prefix + filler * 2)[:64]
    try:
        score = int(analyze_public_key(candidate)["score"])
    except ValueError:
        score = 0
    _EXPECTED_SCORE_CACHE[prefix] = score
    return score


# --- Binary discovery ------------------------------------------------------


def candidate_binary_paths(config: GpuConfig) -> List[Path]:
    paths: List[Path] = []
    override = os.environ.get("MC_KEYGEN_BINARY")
    if override:
        paths.append(Path(override).expanduser())
    for candidate in config.binary_candidates:
        path = Path(candidate).expanduser()
        paths.append(path if path.is_absolute() else PROJECT_DIRECTORY / path)
    discovered = shutil.which("mc-keygen")
    if discovered:
        paths.append(Path(discovered))

    unique: List[Path] = []
    seen: Set[str] = set()
    for path in paths:
        try:
            path = path.resolve()
        except OSError:
            pass
        if str(path) not in seen:
            seen.add(str(path))
            unique.append(path)
    return unique


def find_binary(config: GpuConfig, allow_build: bool = True) -> Optional[Path]:
    if not config.enabled:
        return None
    for path in candidate_binary_paths(config):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    source = PROJECT_DIRECTORY / config.source_directory_name
    cargo = shutil.which("cargo")
    if allow_build and config.auto_build_if_source_present and cargo and (source / "Cargo.toml").is_file():
        features = "cuda" if has_nvidia_gpu() else ""
        command = [cargo, "build", "--release"]
        if features:
            command += ["--features", features]
        completed = subprocess.run(command, cwd=str(source), check=False)
        candidate = source / "target" / "release" / "mc-keygen"
        if completed.returncode == 0 and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def gpu_name() -> Optional[str]:
    """The first NVIDIA GPU's marketing name, or None if there isn't one."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        completed = subprocess.run(
            [smi, "--query-gpu=name", "--format=csv,noheader"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    first = (completed.stdout or "").strip().splitlines()
    return first[0].strip() if first and first[0].strip() else None


def has_nvidia_gpu() -> bool:
    return gpu_name() is not None


def describe_device(device: Optional[str]) -> str:
    """Name a GPU for prose, reading naturally after "an".

    Cards usually report themselves as "NVIDIA GeForce ...", so prefixing
    another "NVIDIA" would stutter; ones that do not get the vendor added.
    """
    if not device:
        return ""
    return device if device.upper().startswith("NVIDIA") else f"NVIDIA {device}"


def detect_capabilities(binary: Path) -> Dict[str, bool]:
    """A CPU-only build rejects --gpu-only outright; adapt instead of failing."""
    try:
        completed = subprocess.run(
            [str(binary), "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"gpu": False, "verify": False, "threads": False, "verify_pairs": False, "harvest": False}
    text = completed.stdout or ""
    return {
        "gpu": "--gpu-only" in text,
        "verify": "--verify" in text,
        "threads": "--threads" in text,
        "verify_pairs": "verify-pairs" in text,
        "harvest": "harvest" in text and "policy-v2" in text,
    }


@dataclass(frozen=True)
class BackendStatus:
    """What the mc-keygen backend is doing, and why, in plain terms."""

    active: bool
    mode: str            # "gpu", "cpu" or "off"
    headline: str        # one line the user can act on
    detail: str          # supporting line
    remedy: Optional[str] = None   # what to do about it, when not active
    harvesting: bool = False


def describe_backend(
    config: GpuConfig,
    binary: Optional[Path],
    capabilities: Optional[Dict[str, bool]],
    device: Optional[str] = None,
    device_probed: bool = False,
) -> BackendStatus:
    """Explain the backend situation without the user having to infer it.

    A binary *built* with CUDA support is not the same thing as a GPU being
    present. Reporting the build's capability as though it were the hardware
    told people their GPU was in use when the machine had none — and, worse,
    led the engine to pass ``--gpu-only`` to a binary that would refuse it.
    """
    if not device_probed:
        device = gpu_name()

    if not config.enabled:
        return BackendStatus(
            False, "off",
            "CPU only - the mc-keygen backend was disabled with --no-gpu",
            "Pattern search from character zero; visible six-character IDs come first.",
            "Drop --no-gpu to use it." + (f" An {describe_device(device)} is present." if device else ""),
        )

    if binary is None:
        if device:
            return BackendStatus(
                False, "off",
                f"CPU only - an {describe_device(device)} is present but mc-keygen is not built",
                "The GPU is idle. Build the native backend to enable accelerated searching.",
                "Run ./install.sh again; it will install what the CUDA build needs.",
            )
        return BackendStatus(
            False, "off",
            "CPU only - no NVIDIA GPU detected and no mc-keygen binary found",
            "Pattern search from character zero; visible six-character IDs come first.",
            None,
        )

    gpu_capable = bool(capabilities and capabilities.get("gpu"))
    harvesting = config.broad_harvest and bool(capabilities and capabilities.get("harvest"))

    if gpu_capable and device:
        return BackendStatus(
            True, "gpu",
            f"CPU + GPU - both engines running on an {describe_device(device)}",
            ("CPU and GPU hunt desirable IDs and longer patterns from character zero." if harvesting else
             "CPU searches every pattern shape; the GPU hunts exact prefixes far faster."),
            None, harvesting=harvesting,
        )

    if gpu_capable:
        # Built for CUDA, but nothing to run it on.
        return BackendStatus(
            True, "cpu",
            "CPU + mc-keygen - no GPU detected, so it searches on the processor",
            ("mc-keygen hunts desirable IDs and longer prefixes on the CPU." if harvesting else
             "mc-keygen is helping with exact prefixes, but without the speed a GPU would add."),
            "If this machine does have an NVIDIA card, check the driver: nvidia-smi should list it.",
            harvesting=harvesting,
        )

    return BackendStatus(
        True, "cpu",
        "CPU + mc-keygen (CPU build) - the GPU kernel is not compiled in",
        ("mc-keygen hunts desirable IDs and longer prefixes on the CPU." if harvesting else
         "mc-keygen is helping with exact prefixes, but on the processor rather than the GPU."),
        (f"An {describe_device(device)} is present. Rebuild with --features cuda to use it: "
         "cargo build --release --features cuda --manifest-path mc-keygen/Cargo.toml")
        if device else None,
        harvesting=harvesting,
    )


# --- Batch key verification ------------------------------------------------


VERIFY_PAIRS_COMMAND = "verify-pairs"
VERIFY_PAIRS_TIMEOUT_SECONDS = 120.0


def supports_pair_verification(binary: Optional[Path]) -> bool:
    if binary is None:
        return False
    return bool(detect_capabilities(binary).get("verify_pairs"))


def verify_pairs(binary: Path, pairs: Sequence[Tuple[str, str]]) -> Set[Tuple[str, str]]:
    """Re-derive a batch of public keys from their private keys, in Rust.

    Keys found on the GPU are produced by advancing a scalar rather than
    hashing a seed, so there is no seed to check them against and the public
    key has to be recomputed by scalar multiplication. Python does that in
    about 95 ms per key; this does it in microseconds.

    Verdicts are matched to requests *by position*, never by public key. Two
    records can carry the same public key with different private keys — that is
    precisely the case where one is a tampered copy — and pairing a verdict
    with the wrong private key would mark the tampered one proven. If the
    backend returns a different number of verdicts than it was asked for,
    nothing is proven.
    """
    if not pairs:
        return set()

    request = "\n".join(
        json.dumps({"public_key": public, "private_key": private})
        for public, private in pairs
    ) + "\n"
    try:
        completed = subprocess.run(
            [str(binary), VERIFY_PAIRS_COMMAND],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=VERIFY_PAIRS_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode != 0:
        return set()

    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if len(lines) != len(pairs):
        return set()

    verified: Set[Tuple[str, str]] = set()
    # strict=True is belt and braces: the lengths were checked above, but a
    # silent mismatch here would pair a verdict with the wrong private key.
    for (public, private), line in zip(pairs, lines, strict=True):
        try:
            verdict = json.loads(line)
        except json.JSONDecodeError:
            return set()
        if not verdict.get("valid"):
            continue
        # Cross-check that the verdict is about the key we asked about.
        if str(verdict.get("public_key", "")).upper() != public.upper():
            return set()
        verified.add((public, private))
    return verified


def make_pair_verifier(binary: Optional[Path]):
    """A callable for storage to hand its seedless records to, or None."""
    if not supports_pair_verification(binary):
        return None

    def verifier(pairs: Sequence[Tuple[str, str]]) -> Set[Tuple[str, str]]:
        return verify_pairs(binary, pairs)

    return verifier


def subprocess_environment(config: GpuConfig) -> Dict[str, str]:
    environment = os.environ.copy()
    if config.device_index:
        environment["CUDA_VISIBLE_DEVICES"] = config.device_index
    if config.cuda_module_loading:
        environment.setdefault("CUDA_MODULE_LOADING", config.cuda_module_loading)
    return environment


def build_command(binary: Path, args: Sequence[str], cpu_ids: Sequence[int]) -> List[str]:
    taskset = shutil.which("taskset")
    if taskset and cpu_ids:
        cpu_list = ",".join(str(cpu_id) for cpu_id in cpu_ids)
        return [taskset, "-c", cpu_list, str(binary), *args]
    return [str(binary), *args]


# --- Progress persistence --------------------------------------------------


def default_progress(config: GpuConfig) -> Dict[str, Any]:
    return {
        "found_prefixes": set(),
        "matches_total": 0,
        "elapsed_seconds_total": 0.0,
        "failures_total": 0,
        "timeouts_total": 0,
        "last_error": None,
        "last_match_at": None,
        "keys_per_second": config.default_keys_per_second,
        "campaign_runs": {},
        "target_runs": {},
        "harvest_attempts_total": 0,
        "harvest_replayed_attempts_total": 0,
        "escalations": 0,
    }


def load_progress(config: Config, known_prefixes: Set[str]) -> Dict[str, Any]:
    progress = default_progress(config.gpu)
    payload = read_verified_json(config.paths.gpu_progress)
    if payload is None:
        return progress
    campaign_runs = payload.get("campaign_runs")
    if not isinstance(campaign_runs, dict):
        campaign_runs = {}
    target_runs = payload.get("target_runs", {})
    if not isinstance(target_runs, dict):
        target_runs = {}
    progress.update({
        "found_prefixes": {p for p in payload.get("found_prefixes", []) if p in known_prefixes},
        "matches_total": int(payload.get("matches_total", 0)),
        "elapsed_seconds_total": float(payload.get("elapsed_seconds_total", 0.0)),
        "failures_total": int(payload.get("failures_total", 0)),
        "timeouts_total": int(payload.get("timeouts_total", 0)),
        "last_error": payload.get("last_error"),
        "last_match_at": payload.get("last_match_at"),
        "keys_per_second": max(1.0, float(payload.get("keys_per_second", config.gpu.default_keys_per_second))),
        "campaign_runs": {str(k): int(v) for k, v in campaign_runs.items()},
        "target_runs": {p: max(0, int(v)) for p, v in target_runs.items() if p in known_prefixes},
        "harvest_attempts_total": max(0, int(payload.get("harvest_attempts_total", 0))),
        "harvest_replayed_attempts_total": max(0, int(payload.get("harvest_replayed_attempts_total", 0))),
        "escalations": int(payload.get("escalations", 0)),
    })
    return progress


def save_progress(progress: Mapping[str, Any], config: Config, target_count: int) -> None:
    payload = add_integrity_hash({
        "state_format_version": STATE_FORMAT_VERSION,
        "source_revision": project_revision(),
        "generated_at": utc_now(),
        "target_count": target_count,
        "found_prefixes": sorted(progress.get("found_prefixes", set())),
        "matches_total": int(progress.get("matches_total", 0)),
        "elapsed_seconds_total": round(float(progress.get("elapsed_seconds_total", 0.0)), 3),
        "failures_total": int(progress.get("failures_total", 0)),
        "timeouts_total": int(progress.get("timeouts_total", 0)),
        "last_error": progress.get("last_error"),
        "last_match_at": progress.get("last_match_at"),
        "keys_per_second": round(float(progress.get("keys_per_second", 0.0)), 3),
        "campaign_runs": dict(progress.get("campaign_runs", {})),
        "target_runs": dict(progress.get("target_runs", {})),
        "harvest_attempts_total": int(progress.get("harvest_attempts_total", 0)),
        "harvest_replayed_attempts_total": int(progress.get("harvest_replayed_attempts_total", 0)),
        "escalations": int(progress.get("escalations", 0)),
    })
    atomic_write_json(config.paths.gpu_progress, payload, private=False)


def campaign_id(campaign: Sequence[str]) -> str:
    return hashlib.sha256("|".join(campaign).encode("ascii")).hexdigest()[:16]


# --- Scheduling ------------------------------------------------------------


def select_campaign(
    targets: Sequence[GpuTarget],
    progress: Mapping[str, Any],
    config: GpuConfig,
    cutoff_score: int = 0,
) -> Tuple[str, ...]:
    """Pick the next batch of prefixes to hunt.

    Rotate by attempts per prefix, then favour the longest affordable targets.
    A successful find changes batch membership; keeping history per prefix
    prevents the remaining targets from masquerading as untried work.
    Expected scores guide selection but cannot bound a random suffix's score.
    """
    found = set(progress.get("found_prefixes", set()))
    unresolved = [target for target in targets if target[0] not in found]
    if not unresolved:
        return ()
    if cutoff_score > 0:
        worthwhile = [t for t in unresolved if expected_target_score(t[0]) >= cutoff_score]
        if worthwhile:
            unresolved = worthwhile

    rate = max(1.0, float(progress.get("keys_per_second", config.default_keys_per_second)))
    runs = progress.get("campaign_runs", {})
    target_runs = progress.get("target_runs", {})
    escalations = min(int(progress.get("escalations", 0)), config.max_escalations)
    budget = config.max_expected_campaign_seconds * (config.escalation_factor ** escalations)

    candidates: List[Tuple[float, int, float, int, Tuple[str, ...]]] = []
    for length in sorted({len(target[0]) for target in unresolved}):
        same_length = [target for target in unresolved if len(target[0]) == length]
        same_length.sort(key=lambda target: (int(target_runs.get(target[0], 0)), -target[1], target[0]))
        for offset in range(0, len(same_length), config.max_prefixes_per_campaign):
            chunk = same_length[offset:offset + config.max_prefixes_per_campaign]
            campaign = tuple(target[0] for target in chunk)
            expected_seconds = (16.0 ** length / max(1, len(campaign))) / rate
            if expected_seconds > budget:
                continue
            # Old progress files only have exact-batch history. Use it until
            # per-prefix history is available; retain it for status/auditing.
            attempts = (sum(int(target_runs.get(prefix, 0)) for prefix in campaign) / len(campaign)
                        if target_runs else int(runs.get(campaign_id(campaign), 0)))
            candidates.append((
                attempts,
                -length,
                expected_seconds,
                -sum(target[1] for target in chunk),
                campaign,
            ))

    if not candidates:
        return ()
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return candidates[0][4]


def mark_targets_found(targets: Sequence[GpuTarget], found: Set[str], public_key_hex: str) -> List[str]:
    newly: List[str] = []
    for prefix, _priority, _description in targets:
        if prefix not in found and public_key_hex.startswith(prefix):
            found.add(prefix)
            newly.append(prefix)
    return newly


# --- Result parsing and verification ---------------------------------------


def _find_value(payload: Any, names: Sequence[str]) -> Any:
    normalized = {name.lower() for name in names}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in normalized:
                return value
        for value in payload.values():
            found = _find_value(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_value(item, names)
            if found is not None:
                return found
    return None


def parse_json_output(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ValueError("mc-keygen produced no JSON output")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        try:
            return json.loads(line.strip())
        except json.JSONDecodeError:
            continue
    raise ValueError("mc-keygen output did not contain JSON")


def normalize_external_private_key(payload: Any, public_key_hex: str) -> Tuple[str, Optional[str]]:
    """Accept any shape mc-keygen might emit, but verify all of them."""
    seed_hex = loose_hex(_find_value(payload, ("seed", "ed25519_seed", "private_seed", "secret_seed")))
    if seed_hex is not None and len(seed_hex) == 64:
        seed = bytes.fromhex(seed_hex)
        if public_key_from_seed(seed).hex().upper() == public_key_hex:
            return meshcore_private_key_from_seed(seed).hex().upper(), seed_hex

    private_hex = loose_hex(_find_value(payload, (
        "private_key", "private_key_hex", "private", "secret_key", "secret_key_hex",
        "secret", "prv_key", "prv_key_hex", "prv",
    )))
    if private_hex is None:
        raise ValueError("mc-keygen JSON did not contain a private key")
    if len(private_hex) == 64:
        seed = bytes.fromhex(private_hex)
        if public_key_from_seed(seed).hex().upper() != public_key_hex:
            raise ValueError("GPU seed does not derive the reported public key")
        return meshcore_private_key_from_seed(seed).hex().upper(), private_hex
    if len(private_hex) != 128:
        raise ValueError("GPU private key must be 32 or 64 bytes")

    raw = bytes.fromhex(private_hex)
    possible_seed = raw[:32]
    if raw[32:].hex().upper() == public_key_hex:
        if public_key_from_seed(possible_seed).hex().upper() != public_key_hex:
            raise ValueError("seed||public GPU key failed verification")
        return meshcore_private_key_from_seed(possible_seed).hex().upper(), possible_seed.hex().upper()
    if public_key_from_meshcore_private(raw).hex().upper() != public_key_hex:
        raise ValueError("expanded GPU private key failed verification")
    return private_hex, None


def parse_result(stdout_text: str, campaign: Sequence[str],
                 targets_by_prefix: Mapping[str, GpuTarget],
                 engine_label: str = "mc-keygen") -> Dict[str, Any]:
    payload = parse_json_output(stdout_text)
    public_key_hex = loose_hex(_find_value(payload, ("public_key", "public_key_hex", "public", "pubkey", "address")))
    if public_key_hex is None or len(public_key_hex) != 64 or is_reserved_public_key(public_key_hex):
        raise ValueError("mc-keygen returned an invalid public key")

    matched_prefix = loose_hex(_find_value(payload, ("matched_prefix", "matched", "prefix", "match")))
    if matched_prefix not in campaign or not public_key_hex.startswith(matched_prefix or ""):
        matched_prefix = next((prefix for prefix in campaign if public_key_hex.startswith(prefix)), None)
    if matched_prefix is None:
        raise ValueError("GPU result does not match the active campaign")

    private_key_hex, seed_hex = normalize_external_private_key(payload, public_key_hex)
    analysis = dict(analyze_public_key(public_key_hex))
    target = targets_by_prefix.get(matched_prefix)
    if target is not None:
        analysis["reasons"] = [
            f"{engine_label} exact prefix '{matched_prefix}' ({target[2]})",
            *analysis["reasons"],
        ][:8]

    def as_number(names, cast):
        raw = _find_value(payload, names)
        try:
            return cast(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "private_key_hex": private_key_hex,
        "seed_hex": seed_hex,
        "public_key_hex": public_key_hex,
        "analysis": analysis,
        "matched_prefix": matched_prefix,
        "gpu_attempts": as_number(("attempts", "keys_checked", "checked"), int),
        "gpu_reported_elapsed_seconds": as_number(("elapsed_secs", "elapsed_seconds", "elapsed"), float),
    }


# --- Engine ----------------------------------------------------------------


class KeygenEngine:
    """Drives mc-keygen in bounded campaigns on a background thread."""

    def __init__(self, binary: Path, config: Config, output_queue: "queue.Queue[Dict[str, Any]]") -> None:
        self.binary = binary
        self.config = config
        self.gpu = config.gpu
        self.output_queue = output_queue
        self.targets = build_targets(config.gpu)
        self.targets_by_prefix = {prefix: (prefix, priority, description)
                                  for prefix, priority, description in self.targets}
        self.capabilities = detect_capabilities(binary)
        # Probed once: a GPU does not appear or vanish mid-run, and asking
        # nvidia-smi repeatedly would be a subprocess per status line.
        self.device = gpu_name()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.progress = load_progress(config, set(self.targets_by_prefix))
        self.current_process: Optional[subprocess.Popen] = None
        self.active_campaign: Tuple[str, ...] = ()
        self.active_started: Optional[float] = None
        self.cutoff_score = 0
        self.thread = threading.Thread(target=self._run, name="meshcore-keygen", daemon=True)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._terminate_current(kill=False)
        self.thread.join(timeout=8.0)
        self._terminate_current(kill=True)
        self.thread.join(timeout=2.0)
        self.save()

    def _terminate_current(self, kill: bool) -> None:
        with self.lock:
            process = self.current_process
        if process is None or process.poll() is not None:
            return
        try:
            if not kill and process.stdin is not None:
                process.stdin.write(b"stop\n")
                process.stdin.flush()
            else:
                process.kill() if kill else process.terminate()
        except OSError:
            pass

    def save(self) -> None:
        save_progress(self.snapshot(), self.config, len(self.targets))

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            snapshot = dict(self.progress)
            snapshot["found_prefixes"] = set(self.progress["found_prefixes"])
            snapshot["campaign_runs"] = dict(self.progress["campaign_runs"])
            snapshot["target_runs"] = dict(self.progress["target_runs"])
            snapshot["active_campaign"] = self.active_campaign
            snapshot["active_started"] = self.active_started
            snapshot["alive"] = self.thread.is_alive()
        return snapshot

    def set_cutoff(self, cutoff_score: int) -> None:
        with self.lock:
            self.cutoff_score = int(cutoff_score)

    def register_public_key(self, public_key_hex: str) -> None:
        with self.lock:
            changed = bool(mark_targets_found(self.targets, self.progress["found_prefixes"], public_key_hex))
        if changed:
            self.save()

    @property
    def mode(self) -> str:
        """What this backend is really doing, not what it was built to do."""
        return "gpu" if self.capabilities.get("gpu") and self.device else "cpu"

    @property
    def harvesting(self) -> bool:
        return self.gpu.broad_harvest and bool(self.capabilities.get("harvest"))

    @property
    def status(self) -> BackendStatus:
        return describe_backend(self.gpu, self.binary, self.capabilities,
                                self.device, device_probed=True)

    def _mode_arguments(self) -> List[str]:
        # Only claim the GPU when there is one. Asking a CUDA build for
        # --gpu-only on a machine with no device makes it exit immediately,
        # which reads as three campaign failures and disables the backend.
        if self.mode == "gpu":
            return ["--gpu-only"]
        # A CPU-only build still helps, but must not starve the Python workers.
        if self.capabilities.get("threads"):
            spare = max(1, len(self.gpu_host_cpu_ids))
            return ["--threads", str(spare)]
        return []

    @property
    def gpu_host_cpu_ids(self):
        return self.config.cpu.gpu_host_cpu_ids

    # -- verification -------------------------------------------------------

    def _verify_backend(self) -> bool:
        if self.harvesting:
            # The new native path performs its own mandatory in-process CUDA
            # self-test, including the filter, multiple threads and overflow.
            return True
        # --verify cross-checks the GPU kernel against the host. With no GPU
        # there is nothing to cross-check.
        if (not self.gpu.run_startup_self_test
                or not self.capabilities.get("verify")
                or self.mode != "gpu"):
            return True
        command = build_command(self.binary, ["A", "--gpu-only", "--verify"], self.gpu_host_cpu_ids)
        try:
            completed = subprocess.run(
                command, cwd=str(PROJECT_DIRECTORY), env=subprocess_environment(self.gpu),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=self.gpu.self_test_timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._record_failure(f"backend verification could not run: {error}")
            return False
        output = "\n".join(p.strip() for p in (completed.stdout, completed.stderr) if p and p.strip())
        if completed.returncode != 0:
            self._record_failure("backend verification failed: " + (output[-1000:] or str(completed.returncode)))
            return False
        self._emit("status", f"mc-keygen {self.mode} backend verification passed")
        return True

    # -- main loop ----------------------------------------------------------

    def _run(self) -> None:
        if not self._verify_backend():
            self._emit("disabled", "backend disabled because startup verification failed")
            return

        consecutive_failures = 0
        while not self.stop_event.is_set():
            if self.harvesting:
                outcome = self._run_harvest()
                if self.stop_event.is_set():
                    break
                if outcome == "failure":
                    consecutive_failures += 1
                    if consecutive_failures >= self.gpu.max_consecutive_failures:
                        self._emit("disabled", "harvesting disabled after repeated failures")
                        break
                    self.stop_event.wait(self.gpu.retry_delay_seconds)
                else:
                    consecutive_failures = 0
                continue
            campaign = self._next_campaign()
            if not campaign:
                if not self._escalate():
                    self._emit("status", "every reachable target has been found")
                    self.stop_event.wait(300.0)
                continue

            outcome = self._run_campaign(campaign)
            if self.stop_event.is_set():
                break
            if outcome == "timeout":
                continue
            if outcome == "failure":
                consecutive_failures += 1
                if consecutive_failures >= self.gpu.max_consecutive_failures:
                    self._emit("disabled", "backend disabled after repeated failures")
                    break
                self.stop_event.wait(self.gpu.retry_delay_seconds)
                continue
            consecutive_failures = 0

        with self.lock:
            self.active_campaign = ()
            self.active_started = None
            self.current_process = None
        self.save()

    def _next_campaign(self) -> Tuple[str, ...]:
        with self.lock:
            cutoff = self.cutoff_score
            campaign = select_campaign(self.targets, self.progress, self.gpu, cutoff)
            self.active_campaign = campaign
            self.active_started = time.monotonic() if campaign else None
        return campaign

    def _harvest_event(self, payload: Any, previous: Tuple[int, float, int], policy_cutoff: int) -> Tuple[int, float, int]:
        if not isinstance(payload, dict):
            raise ValueError("harvest event must be an object")
        kind = payload.get("type")
        if kind in ("stats", "done"):
            attempts = payload.get("attempts")
            elapsed = payload.get("elapsed_secs")
            replayed = payload.get("replayed_attempts")
            if (type(attempts) is not int or not previous[0] <= attempts < 2**64
                    or type(replayed) is not int or not previous[2] <= replayed < 2**64
                    or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed)
                    or elapsed <= 0 or elapsed < previous[1]):
                raise ValueError("invalid or decreasing harvest statistics")
            with self.lock:
                self.progress["harvest_attempts_total"] += attempts - previous[0]
                self.progress["harvest_replayed_attempts_total"] += replayed - previous[2]
                window = elapsed - previous[1]
                if window > 0:
                    rate = (attempts - previous[0]) / window
                    alpha = self.gpu.rate_ewma_alpha
                    self.progress["keys_per_second"] = (rate if previous[1] == 0 else
                        self.progress["keys_per_second"] * (1 - alpha) + rate * alpha)
            return attempts, float(elapsed), replayed
        if kind != "match":
            raise ValueError("unknown harvest event type")
        public = loose_hex(payload.get("public_key"))
        if public is None or len(public) != 64 or is_reserved_public_key(public):
            raise ValueError("invalid harvested public key")
        analysis = dict(analyze_public_key(public))
        with self.lock:
            cutoff = max(policy_cutoff, self.cutoff_score)
        if analysis["score"] < cutoff:
            return previous  # Native screening is conservative; Python ranks.
        private, seed = normalize_external_private_key(payload, public)
        analysis["reasons"] = [f"{self.mode.upper()} prefix-pattern harvesting", *analysis["reasons"]][:8]
        self._record_match({
            "private_key_hex": private, "seed_hex": seed, "public_key_hex": public,
            "analysis": analysis, "matched_prefix": None,
        }, 0.0)
        return previous

    def _run_harvest(self) -> str:
        """Drain bounded JSON lines continuously; stop via stdin and save all hits."""
        with self.lock:
            policy_cutoff = max(self.cutoff_score, int(self.gpu.harvest_minimum_bits * 1_000_000))
            self.active_campaign = ()
            self.active_started = time.monotonic()
        started = time.monotonic()
        previous = (0, 0.0, 0)
        done = False
        stop_sent = None
        process = None
        failure = None
        try:
            with tempfile.TemporaryDirectory(prefix="meshcore-policy-") as directory, tempfile.TemporaryFile() as errors:
                policy_file = Path(directory) / "policy.json"
                policy_file.write_text(json.dumps(build_policy(policy_cutoff)), encoding="ascii")
                command = build_command(self.binary, [
                    "harvest", "--policy", str(policy_file), "--seconds",
                    str(min(86400.0, self.gpu.campaign_time_slice_seconds)), "--stdin-stop",
                    *self._mode_arguments(),
                ], self.gpu_host_cpu_ids)
                process = subprocess.Popen(command, cwd=str(PROJECT_DIRECTORY),
                    env=subprocess_environment(self.gpu), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=errors)
                with self.lock:
                    self.current_process = process
                os.set_blocking(process.stdout.fileno(), False)
                pending = b""
                deadline = started + self.gpu.campaign_time_slice_seconds + self.gpu.self_test_timeout_seconds + 30
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        now = time.monotonic()
                        if (self.stop_event.is_set() or now >= deadline) and stop_sent is None:
                            self._terminate_current(kill=False)
                            stop_sent = now
                        if stop_sent is not None and now - stop_sent > 6:
                            self._terminate_current(kill=True)
                            raise ValueError("harvester did not stop gracefully")
                        if not selector.select(timeout=0.1):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if len(line) > 16384:
                                raise ValueError("oversized harvest event")
                            if done:
                                raise ValueError("harvest event after completion")
                            payload = json.loads(line)
                            previous = self._harvest_event(payload, previous, policy_cutoff)
                            done = payload.get("type") == "done"
                        if len(pending) > 16384:
                            raise ValueError("oversized harvest event")
                return_code = process.wait(timeout=3)
                if pending or not done or return_code != 0:
                    # Do not echo the stdout stream: it carries private keys.
                    errors.seek(0, os.SEEK_END)
                    size = errors.tell()
                    errors.seek(max(0, size - 1000))
                    detail = errors.read().decode("utf-8", "replace").strip()
                    raise ValueError("harvest stream did not finish cleanly" + (f": {detail}" if detail else ""))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failure = str(error)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process.stdout is not None:
                    process.stdout.close()
            with self.lock:
                self.current_process = None
                self.active_started = None
                self.progress["elapsed_seconds_total"] += time.monotonic() - started
            self.save()
        if failure is not None:
            self._record_failure(f"prefix-pattern harvesting failed: {failure}")
            return "failure"
        return "stopped" if self.stop_event.is_set() else "complete"

    def _escalate(self) -> bool:
        """Raise the difficulty budget instead of idling with the device free."""
        with self.lock:
            if self.progress.get("escalations", 0) >= self.gpu.max_escalations:
                return False
            self.progress["escalations"] = int(self.progress.get("escalations", 0)) + 1
            level = self.progress["escalations"]
        self.save()
        self._emit("status", f"all affordable targets found; raising the difficulty budget (level {level})")
        return True

    def _run_campaign(self, campaign: Tuple[str, ...]) -> str:
        identifier = campaign_id(campaign)
        command = build_command(
            self.binary, [*campaign, "--json", *self._mode_arguments()], self.gpu_host_cpu_ids
        )
        started = time.monotonic()
        timed_out = False

        with tempfile.TemporaryFile(mode="w+b") as out_file, tempfile.TemporaryFile(mode="w+b") as err_file:
            try:
                process = subprocess.Popen(
                    command, cwd=str(PROJECT_DIRECTORY), env=subprocess_environment(self.gpu),
                    stdout=out_file, stderr=err_file,
                )
            except OSError as error:
                self._record_failure(f"could not start mc-keygen: {error}")
                return "failure"

            with self.lock:
                self.current_process = process
                runs = self.progress["campaign_runs"]
                runs[identifier] = int(runs.get(identifier, 0)) + 1
                target_runs = self.progress["target_runs"]
                for prefix in campaign:
                    target_runs[prefix] = int(target_runs.get(prefix, 0)) + 1

            deadline = started + self.gpu.campaign_time_slice_seconds
            while process.poll() is None and not self.stop_event.wait(0.25):
                if time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    break
            if process.poll() is None:
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait(timeout=2.0)

            return_code = process.poll()
            elapsed = time.monotonic() - started
            out_file.seek(0)
            err_file.seek(0)
            stdout_text = out_file.read().decode("utf-8", "replace")
            stderr_text = err_file.read().decode("utf-8", "replace")
            with self.lock:
                self.current_process = None
                self.active_started = None
                self.progress["elapsed_seconds_total"] += elapsed

        if self.stop_event.is_set():
            return "stopped"
        if timed_out:
            with self.lock:
                self.progress["timeouts_total"] += 1
            self.save()
            self._emit("status", f"rotating campaign len={len(campaign[0])} n={len(campaign)} after {elapsed:.0f}s")
            return "timeout"
        if return_code != 0:
            message = stderr_text.strip() or stdout_text.strip() or f"exit code {return_code}"
            self._record_failure(f"mc-keygen failed: {message[-1000:]}")
            return "failure"

        try:
            parsed = parse_result(stdout_text, campaign, self.targets_by_prefix,
                                  "GPU" if self.mode == "gpu" else "mc-keygen")
        except Exception as error:
            self._record_failure(f"could not validate mc-keygen result: {error}")
            return "failure"

        self._record_match(parsed, elapsed)
        return "match"

    def _record_match(self, parsed: Dict[str, Any], elapsed: float) -> None:
        attempts = parsed.get("gpu_attempts")
        reported = parsed.get("gpu_reported_elapsed_seconds")
        measured_rate: Optional[float] = None
        if isinstance(attempts, int) and attempts > 0:
            window = float(reported) if isinstance(reported, (int, float)) and reported > 0 else elapsed
            if window > 0:
                measured_rate = attempts / window

        with self.lock:
            newly = mark_targets_found(self.targets, self.progress["found_prefixes"], parsed["public_key_hex"])
            self.progress["matches_total"] += 1
            self.progress["last_error"] = None
            self.progress["last_match_at"] = utc_now()
            if measured_rate is not None:
                previous = float(self.progress.get("keys_per_second", self.gpu.default_keys_per_second))
                alpha = self.gpu.rate_ewma_alpha
                self.progress["keys_per_second"] = previous * (1.0 - alpha) + measured_rate * alpha
        self.save()

        parsed.update({
            "type": "keygen_result",
            "found_timestamp": time.time(),
            "gpu_elapsed_seconds": elapsed,
            "newly_completed_targets": newly,
            "gpu_measured_keys_per_second": measured_rate,
        })
        self.output_queue.put(parsed)

    def _record_failure(self, message: str) -> None:
        with self.lock:
            self.progress["failures_total"] += 1
            self.progress["last_error"] = message
        self.save()
        self._emit("error", message)

    def _emit(self, kind: str, message: str) -> None:
        self.output_queue.put({"type": f"keygen_{kind}", "message": message})
