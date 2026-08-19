#!/usr/bin/env python3
"""
MeshCore Vanity Harvester.

Runs continuously with no arguments. It combines:

* Generic CPU discovery for arbitrary words, repetitions, runs, sequences,
  palindromes and other patterns.
* Optional NVIDIA CUDA exact-prefix campaigns through mc-keygen.
* Full 64-character public-key scoring dominated by estimated mathematical
  rarity rather than ad-hoc capped point growth.
* Persistent top-500 lists:
    - one best key per six-character MeshCore repeater ID;
    - an unrestricted vanity hall of fame;
    - independent word, single-run and periodic-pattern boards.

Private-key state is secret. Anyone possessing a saved private key can use that
MeshCore identity.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import math
import multiprocessing as mp
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# =============================================================================
# USER CONFIGURATION
# =============================================================================

try:
    AVAILABLE_CPU_IDS = tuple(sorted(os.sched_getaffinity(0)))
except (AttributeError, OSError):
    AVAILABLE_CPU_IDS = tuple(range(os.cpu_count() or 2))
LOGICAL_CPUS = len(AVAILABLE_CPU_IDS)
RESERVED_LOGICAL_CPUS = min(4, max(1, LOGICAL_CPUS // 4))
CPU_WORKER_CPU_IDS = AVAILABLE_CPU_IDS[:max(1, LOGICAL_CPUS - RESERVED_LOGICAL_CPUS)]
GPU_HOST_CPU_IDS = AVAILABLE_CPU_IDS[len(CPU_WORKER_CPU_IDS):] or AVAILABLE_CPU_IDS[-1:]
CPU_WORKERS = len(CPU_WORKER_CPU_IDS)
CPU_RANDOM_SEED_BATCH = 512
CPU_PIN_WORKERS = hasattr(os, "sched_setaffinity")
CPU_NICE_INCREMENT = 5

GPU_ENABLED = True
GPU_RUN_STARTUP_SELF_TEST = True
GPU_SELF_TEST_TIMEOUT_SECONDS = 120.0
GPU_DEVICE_INDEX = "0"
GPU_CUDA_MODULE_LOADING = "EAGER"
GPU_MAX_PREFIXES_PER_CAMPAIGN = 16
GPU_MIN_TARGET_LENGTH = 9
GPU_MAX_TARGET_LENGTH = 12
GPU_CAMPAIGN_TIME_SLICE_SECONDS = 15 * 60.0
GPU_MAX_EXPECTED_CAMPAIGN_SECONDS = 6 * 60 * 60.0
GPU_DEFAULT_KEYS_PER_SECOND = 10_000_000.0
GPU_RATE_EWMA_ALPHA = 0.30
GPU_RETRY_DELAY_SECONDS = 5.0
GPU_MAX_CONSECUTIVE_FAILURES = 3
GPU_AUTO_BUILD_IF_SOURCE_PRESENT = True
GPU_SOURCE_DIRECTORY_NAME = "mc-keygen"
GPU_PROGRESS_FILENAME = "gpu_campaign_state.json"
GPU_BINARY_CANDIDATES = (
    "mc-keygen/target/release/mc-keygen",
    "mc-keygen",
)

TOP_REPEATER_IDS = 500
TOP_VANITY_KEYS = 500
TOP_CATEGORY_KEYS = 500
CATEGORY_BOARD_FAMILIES = ("word", "single_run", "periodic")
REPEATER_ID_HEX_LENGTH = 6
PUBLIC_KEY_HEX_LENGTH = 64

# Rarity is measured in bits. One additional rarity bit always outranks all
# subjective bonuses combined. This preserves the mathematical ordering.
RARITY_SCORE_PER_BIT = 1_000_000
MAX_PRIMARY_SUBJECTIVE_BONUS = 700_000
MAX_SECONDARY_BONUS = 250_000
MAX_TOTAL_TIEBREAKER_BONUS = MAX_PRIMARY_SUBJECTIVE_BONUS + MAX_SECONDARY_BONUS
INITIAL_MINIMUM_SCORE = 16 * RARITY_SCORE_PER_BIT

STATUS_INTERVAL_SECONDS = 30.0
CHECKPOINT_INTERVAL_SECONDS = 30.0
WORKER_HEALTH_INTERVAL_SECONDS = 5.0
COUNTER_BATCH_SIZE = 10_000
RESULT_QUEUE_SIZE = 4_096
URGENT_CHECKPOINT_SCORE = 28 * RARITY_SCORE_PER_BIT
URGENT_CHECKPOINT_MIN_INTERVAL_SECONDS = 1.0
MAX_RUNTIME_SECONDS = 0
MAX_WORKER_RESTARTS = 5
WORKER_RESTART_WINDOW_SECONDS = 300.0

DATA_DIRECTORY_NAME = "data"
PUBLIC_STATE_FILENAME = "leaderboards_public.json"
PRIVATE_STATE_FILENAME = "leaderboards_private.json"
HISTORY_FILENAME = "leaderboard_history.private.jsonl"
PUBLIC_ID_LIST_FILENAME = "repeater_ids_public.jsonl"
PRIVATE_KEY_MAP_FILENAME = "private_keys_by_public_key.json"

# Exact preferences. These add semantic/aesthetic value, while rarity remains
# the dominant factor. Only Montreal's 514 area code is included.
PREFERRED_EXACT_PREFIXES: Mapping[str, Tuple[int, str]] = {
    "444444": (240_000, "preferred all-4 repeater ID"),
    "514514": (250_000, "Montreal 514 repeated"),
    "ABCDEF": (205_000, "ascending hexadecimal alphabet"),
    "FEDCBA": (205_000, "descending hexadecimal alphabet"),
    "123456": (195_000, "ascending numeric sequence"),
    "654321": (195_000, "descending numeric sequence"),
}

LOCAL_EXACT_PREFIXES: Mapping[str, Tuple[int, str]] = {
    "514514": (250_000, "Montreal 514 repeated"),
    "514514514": (285_000, "Montreal 514 repeated three times"),
    "514C0FFEE": (285_000, "Montreal 514 + COFFEE"),
    "514FACADE": (285_000, "Montreal 514 + FAÇADE"),
    "514CAFE": (245_000, "Montreal 514 + CAFÉ"),
    "514BEBE": (235_000, "Montreal 514 + BÉBÉ"),
    "514DECAF": (240_000, "Montreal 514 + DÉCAF"),
    "514C07E": (230_000, "Montreal 514 + CÔTE"),
    "514FACE": (225_000, "Montreal 514 + FACE"),
    "C0FFEE514": (220_000, "COFFEE + Montreal 514"),
    "FACADE514": (220_000, "FAÇADE + Montreal 514"),
    "CAFE514": (190_000, "CAFÉ + Montreal 514"),
}

_HEXSPEAK_SUBSTITUTIONS: Mapping[str, str] = {
    "A": "A", "B": "B", "C": "C", "D": "D", "E": "E", "F": "F",
    "G": "6", "I": "1", "L": "1", "O": "0", "S": "5", "T": "7", "Z": "2",
}

# Quality is only a sub-bit tie-breaker. Length-derived rarity remains dominant.
VANITY_WORD_SPECS: Sequence[Tuple[str, str, int]] = (
    # English
    ("coffee", "English", 34_000), ("office", "English", 31_000),
    ("decode", "English", 30_000), ("code", "English", 15_000),
    ("coded", "English", 20_000), ("database", "English", 36_000),
    ("data", "English", 16_000), ("deface", "English", 29_000),
    ("defaced", "English", 32_000), ("efface", "English", 29_000),
    ("effaced", "English", 32_000), ("facade", "English/French", 32_000),
    ("decade", "English/French", 30_000), ("deceased", "English", 35_000),
    ("cease", "English", 23_000), ("ceased", "English", 26_000),
    ("debase", "English", 25_000), ("debased", "English", 28_000),
    ("access", "English/French", 27_000), ("accessed", "English", 31_000),
    ("badass", "English leetspeak", 28_000), ("beaded", "English", 24_000),
    ("dabbed", "English", 24_000), ("beefed", "English", 23_000),
    ("deadbeef", "English iconic hexspeak", 38_000),
    ("feedface", "English iconic hexspeak", 38_000),
    ("cafebabe", "English/French iconic hexspeak", 37_000),
    ("decaf", "English/French", 19_000), ("dead", "English", 13_000),
    ("beef", "English", 14_000), ("feed", "English", 13_000),
    ("face", "English/French", 14_000), ("cafe", "English/French", 15_000),
    ("babe", "English", 12_000), ("deaf", "English", 12_000),
    ("fade", "English", 11_000), ("deed", "English", 11_000),
    ("safe", "English", 12_000), ("case", "English", 11_000),
    ("base", "English", 11_000), ("basic", "English", 16_000),
    ("seed", "English", 11_000), ("dad", "English", 6_000),
    ("bad", "English", 6_000), ("ace", "English", 6_000),
    ("bee", "English", 5_000), ("fee", "English", 5_000),
    ("bed", "English", 5_000), ("cab", "English/French", 5_000),

    # French
    ("café", "French", 16_000), ("bébé", "French", 15_000),
    ("façade", "French", 32_000), ("efface", "French", 29_000),
    ("effacée", "French", 33_000), ("accède", "French", 29_000),
    ("accès", "French", 22_000), ("cède", "French", 15_000),
    ("cédée", "French", 21_000), ("décède", "French", 30_000),
    ("décédé", "French", 30_000), ("décès", "French", 22_000),
    ("cesse", "French", 22_000), ("cessé", "French", 22_000),
    ("abaissé", "French", 27_000), ("abaissée", "French", 30_000),
    ("blessé", "French", 25_000), ("blessée", "French", 28_000),
    ("décafé", "French", 20_000), ("côte", "French", 16_000),
    ("bête", "French", 14_000), ("fête", "French", 14_000),
    ("tête", "French", 14_000), ("été", "French", 10_000),
    ("assez", "French", 18_000), ("dada", "French", 11_000),
    ("bac", "French", 6_000),

)


# =============================================================================
# INTERNAL CONSTANTS AND TYPES
# =============================================================================

SCRIPT_VERSION = "8.0.0"
SCORE_VERSION = 8
STATE_FORMAT_VERSION = 6
HEX_DIGITS = frozenset("0123456789ABCDEF")

BASE_DIRECTORY = Path(__file__).resolve().parent
if (BASE_DIRECTORY / GPU_SOURCE_DIRECTORY_NAME).is_dir():
    DEFAULT_DATA_DIRECTORY = BASE_DIRECTORY / DATA_DIRECTORY_NAME
else:
    DEFAULT_DATA_DIRECTORY = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ) / "meshcore-vanity-harvester"
DATA_DIRECTORY = DEFAULT_DATA_DIRECTORY
PUBLIC_STATE_PATH = DATA_DIRECTORY / PUBLIC_STATE_FILENAME
PRIVATE_STATE_PATH = DATA_DIRECTORY / PRIVATE_STATE_FILENAME
PUBLIC_BACKUP_PATH = DATA_DIRECTORY / f"{PUBLIC_STATE_FILENAME}.bak"
PRIVATE_BACKUP_PATH = DATA_DIRECTORY / f"{PRIVATE_STATE_FILENAME}.bak"
HISTORY_PATH = DATA_DIRECTORY / HISTORY_FILENAME
LOCK_PATH = DATA_DIRECTORY / ".harvester.lock"
GPU_PROGRESS_PATH = DATA_DIRECTORY / GPU_PROGRESS_FILENAME
GPU_PROGRESS_BACKUP_PATH = DATA_DIRECTORY / f"{GPU_PROGRESS_FILENAME}.bak"
PUBLIC_ID_LIST_PATH = DATA_DIRECTORY / PUBLIC_ID_LIST_FILENAME
PUBLIC_ID_LIST_BACKUP_PATH = DATA_DIRECTORY / f"{PUBLIC_ID_LIST_FILENAME}.bak"
PRIVATE_KEY_MAP_PATH = DATA_DIRECTORY / PRIVATE_KEY_MAP_FILENAME
PRIVATE_KEY_MAP_BACKUP_PATH = DATA_DIRECTORY / f"{PRIVATE_KEY_MAP_FILENAME}.bak"

LEGACY_PRIVATE_PATHS = (
    BASE_DIRECTORY / "meshcore_vanity_hybrid_v6_data" / "leaderboard_top50_private.json",
    BASE_DIRECTORY / "meshcore_vanity_hybrid_v5_data" / "leaderboard_top50_private.json",
    BASE_DIRECTORY / "meshcore_vanity_harvester_v4_data" / "leaderboard_top50_private.json",
    BASE_DIRECTORY / "meshcore_vanity_top50_private.json",
)

Record = Dict[str, Any]
UniqueLeaderboard = Dict[str, Record]
HallLeaderboard = Dict[str, Record]
CategoryLeaderboards = Dict[str, HallLeaderboard]
GpuTarget = Tuple[str, int, str]


def set_data_directory(path: Path) -> None:
    global DATA_DIRECTORY, PUBLIC_STATE_PATH, PRIVATE_STATE_PATH
    global PUBLIC_BACKUP_PATH, PRIVATE_BACKUP_PATH, HISTORY_PATH, LOCK_PATH
    global GPU_PROGRESS_PATH, GPU_PROGRESS_BACKUP_PATH
    global PUBLIC_ID_LIST_PATH, PUBLIC_ID_LIST_BACKUP_PATH
    global PRIVATE_KEY_MAP_PATH, PRIVATE_KEY_MAP_BACKUP_PATH

    DATA_DIRECTORY = path.expanduser().resolve()
    PUBLIC_STATE_PATH = DATA_DIRECTORY / PUBLIC_STATE_FILENAME
    PRIVATE_STATE_PATH = DATA_DIRECTORY / PRIVATE_STATE_FILENAME
    PUBLIC_BACKUP_PATH = DATA_DIRECTORY / f"{PUBLIC_STATE_FILENAME}.bak"
    PRIVATE_BACKUP_PATH = DATA_DIRECTORY / f"{PRIVATE_STATE_FILENAME}.bak"
    HISTORY_PATH = DATA_DIRECTORY / HISTORY_FILENAME
    LOCK_PATH = DATA_DIRECTORY / ".harvester.lock"
    GPU_PROGRESS_PATH = DATA_DIRECTORY / GPU_PROGRESS_FILENAME
    GPU_PROGRESS_BACKUP_PATH = DATA_DIRECTORY / f"{GPU_PROGRESS_FILENAME}.bak"
    PUBLIC_ID_LIST_PATH = DATA_DIRECTORY / PUBLIC_ID_LIST_FILENAME
    PUBLIC_ID_LIST_BACKUP_PATH = DATA_DIRECTORY / f"{PUBLIC_ID_LIST_FILENAME}.bak"
    PRIVATE_KEY_MAP_PATH = DATA_DIRECTORY / PRIVATE_KEY_MAP_FILENAME
    PRIVATE_KEY_MAP_BACKUP_PATH = DATA_DIRECTORY / f"{PRIVATE_KEY_MAP_FILENAME}.bak"


@dataclass(frozen=True)
class PatternFeature:
    kind: str
    start: int
    length: int
    rarity_bits: float
    semantic_bonus: int
    description: str


def pattern_family(kind: str) -> str:
    if kind in {"word", "word_repeat", "word_extension", "compound_words"}:
        return "word"
    if kind in {"single_run", "specific_run", "run"}:
        return "single_run"
    if kind in {"periodic", "structured_id"}:
        return "periodic"
    if kind in {"sequence", "specific_sequence"}:
        return "sequence"
    if kind == "palindrome":
        return "palindrome"
    return "special"


# =============================================================================
# WORD CATALOG
# =============================================================================


def _normalized_plain_word(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    ascii_word = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in ascii_word.upper() if c.isalpha())


def encode_hexspeak(word: str) -> Optional[Tuple[str, int]]:
    normalized = _normalized_plain_word(word)
    if not normalized:
        return None
    encoded: List[str] = []
    substitutions = 0
    for character in normalized:
        replacement = _HEXSPEAK_SUBSTITUTIONS.get(character)
        if replacement is None:
            return None
        encoded.append(replacement)
        substitutions += replacement != character
    return "".join(encoded), substitutions


def build_hex_words() -> Dict[str, Tuple[int, str]]:
    qualities: Dict[str, int] = {}
    descriptions: Dict[str, List[str]] = {}
    for plain_word, language, quality in VANITY_WORD_SPECS:
        encoded_result = encode_hexspeak(plain_word)
        if encoded_result is None:
            continue
        encoded, substitutions = encoded_result
        if len(encoded) < 3 or encoded.startswith(("00", "FF")):
            continue
        score = int(quality) + (4_000 if substitutions == 0 else 0)
        qualities[encoded] = max(qualities.get(encoded, 0), score)
        label = f"{language}: {plain_word}"
        if substitutions:
            label += f" → {encoded}"
        descriptions.setdefault(encoded, [])
        if label not in descriptions[encoded]:
            descriptions[encoded].append(label)
    return {
        word: (qualities[word], "; ".join(descriptions[word]))
        for word in sorted(qualities)
    }


HEX_WORDS: Mapping[str, Tuple[int, str]] = build_hex_words()
WORDS_BY_FIRST: Dict[str, Tuple[str, ...]] = {}
for _first in HEX_DIGITS:
    WORDS_BY_FIRST[_first] = tuple(
        sorted(
            (word for word in HEX_WORDS if word.startswith(_first)),
            key=lambda word: (-len(word), -HEX_WORDS[word][0], word),
        )
    )
WORD_LENGTHS_DESC = tuple(sorted({len(word) for word in HEX_WORDS}, reverse=True))
WORD_SETS_BY_LENGTH: Mapping[int, frozenset] = {
    length: frozenset(word for word in HEX_WORDS if len(word) == length)
    for length in WORD_LENGTHS_DESC
}
WORD_REGEX_BY_LENGTH: Mapping[int, re.Pattern[str]] = {
    length: re.compile("|".join(sorted((re.escape(word) for word in words), key=len, reverse=True)))
    for length, words in WORD_SETS_BY_LENGTH.items()
}


_QUICK_PERIODIC_REGEX = re.compile(r"(?:(..)(?:\1){2}|(.{3,12})\2)")
_QUICK_EVEN_PALINDROME_REGEX = re.compile(r"(?=([0-9A-F])([0-9A-F])([0-9A-F])\3\2\1)")
_QUICK_ODD_PALINDROME_REGEX = re.compile(r"(?=([0-9A-F])([0-9A-F])([0-9A-F])[0-9A-F]\3\2\1)")
_QUICK_RUN_REGEX = re.compile(r"([0-9A-F])\1{3,}")
_QUICK_SEQUENCE_REGEX = re.compile("|".join(
    ["0123456789ABCDEF"[index:index + 5] for index in range(12)]
    + ["FEDCBA9876543210"[index:index + 5] for index in range(12)]
))


# =============================================================================
# GENERAL HELPERS
# =============================================================================


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def add_integrity_hash(payload: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(payload)
    result.pop("integrity_sha256", None)
    result["integrity_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def verify_integrity_hash(payload: Mapping[str, Any]) -> bool:
    expected = payload.get("integrity_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    unsigned = dict(payload)
    unsigned.pop("integrity_sha256", None)
    return hmac.compare_digest(hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest(), expected)


def ensure_secure_directory() -> None:
    DATA_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DATA_DIRECTORY, 0o700)


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, backup_path: Path, payload: Mapping[str, Any], private: bool) -> None:
    mode = 0o600 if private else 0o644
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    backup_temporary = backup_path.with_name(f".{backup_path.name}.tmp.{os.getpid()}")

    if path.exists():
        try:
            with path.open("rb") as source, backup_temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(backup_temporary, mode)
            os.replace(backup_temporary, backup_path)
        finally:
            try:
                backup_temporary.unlink()
            except FileNotFoundError:
                pass

    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
        if backup_path.exists():
            os.chmod(backup_path, mode)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, backup_path: Path, content: str, private: bool) -> None:
    mode = 0o600 if private else 0o644
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    backup_temporary = backup_path.with_name(f".{backup_path.name}.tmp.{os.getpid()}")
    if path.exists():
        try:
            with path.open("rb") as source, backup_temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(backup_temporary, mode)
            os.replace(backup_temporary, backup_path)
        finally:
            try:
                backup_temporary.unlink()
            except FileNotFoundError:
                pass
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
        if backup_path.exists():
            os.chmod(backup_path, mode)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def acquire_single_instance_lock():
    ensure_secure_directory()
    lock_file = LOCK_PATH.open("a+", encoding="utf-8")
    os.chmod(LOCK_PATH, 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError(f"another harvester is already using {DATA_DIRECTORY}")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()} started={utc_now()}\n")
    lock_file.flush()
    os.fsync(lock_file.fileno())
    return lock_file


def normalize_hex(value: Any, expected_length: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if len(normalized) != expected_length or any(c not in HEX_DIGITS for c in normalized):
        return None
    return normalized


# =============================================================================
# ED25519 / MESHCORE KEY HELPERS
# =============================================================================


def meshcore_private_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must contain exactly 32 bytes")
    expanded = bytearray(hashlib.sha512(seed).digest())
    expanded[0] &= 0xF8
    expanded[31] &= 0x7F
    expanded[31] |= 0x40
    return bytes(expanded)


def public_key_from_seed(seed: bytes) -> bytes:
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


_ED25519_Q = 2**255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_Q - 2, _ED25519_Q)) % _ED25519_Q
_ED25519_I = pow(2, (_ED25519_Q - 1) // 4, _ED25519_Q)


def _ed25519_xrecover(y: int) -> int:
    xx = ((y * y - 1) * pow(_ED25519_D * y * y + 1, _ED25519_Q - 2, _ED25519_Q)) % _ED25519_Q
    x = pow(xx, (_ED25519_Q + 3) // 8, _ED25519_Q)
    if (x * x - xx) % _ED25519_Q != 0:
        x = (x * _ED25519_I) % _ED25519_Q
    if x & 1:
        x = _ED25519_Q - x
    return x


_ED25519_BASE_Y = (4 * pow(5, _ED25519_Q - 2, _ED25519_Q)) % _ED25519_Q
_ED25519_BASE = (_ed25519_xrecover(_ED25519_BASE_Y), _ED25519_BASE_Y)


def _ed25519_add(left: Tuple[int, int], right: Tuple[int, int]) -> Tuple[int, int]:
    x1, y1 = left
    x2, y2 = right
    product = (_ED25519_D * x1 * x2 * y1 * y2) % _ED25519_Q
    x3 = ((x1 * y2 + x2 * y1) * pow(1 + product, _ED25519_Q - 2, _ED25519_Q)) % _ED25519_Q
    y3 = ((y1 * y2 + x1 * x2) * pow(1 - product, _ED25519_Q - 2, _ED25519_Q)) % _ED25519_Q
    return x3, y3


def _ed25519_scalar_mult(point: Tuple[int, int], scalar: int) -> Tuple[int, int]:
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _ed25519_add(result, addend)
        addend = _ed25519_add(addend, addend)
        scalar >>= 1
    return result


def public_key_from_meshcore_private(private_key: bytes) -> bytes:
    if len(private_key) != 64:
        raise ValueError("MeshCore private key must contain exactly 64 bytes")
    scalar_bytes = private_key[:32]
    if scalar_bytes[0] & 0x07 or scalar_bytes[31] & 0x80 or not scalar_bytes[31] & 0x40:
        raise ValueError("MeshCore Ed25519 scalar is not clamped")
    scalar = int.from_bytes(scalar_bytes, "little")
    x, y = _ed25519_scalar_mult(_ED25519_BASE, scalar)
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


# =============================================================================
# FULL 64-CHARACTER RARITY SCORING
# =============================================================================


def _location_penalty_bits(length: int, start: int) -> float:
    if start == 0:
        return 0.0
    possible_positions = max(1, PUBLIC_KEY_HEX_LENGTH - length + 1)
    return math.log2(possible_positions)


def _position_bonus(start: int, length: int) -> int:
    if start == 0:
        return 330_000 + min(120_000, max(0, length - 6) * 8_000)
    if start < REPEATER_ID_HEX_LENGTH:
        return max(100_000, 240_000 - start * 28_000)
    return max(0, 75_000 - start * 2_500)


def _feature_subjective_bonus(feature: PatternFeature) -> int:
    kind_bonus = {
        "local_exact": 190_000,
        "preferred_exact": 180_000,
        "word_repeat": 150_000,
        "word_extension": 145_000,
        "compound_words": 140_000,
        "word": 125_000,
        "specific_sequence": 120_000,
        "specific_run": 250_000,
        "single_run": 250_000,
        "periodic": 180_000,
        "sequence": 100_000,
        "palindrome": 90_000,
        "structured_id": 100_000,
    }.get(feature.kind, 30_000)
    return min(
        MAX_PRIMARY_SUBJECTIVE_BONUS,
        max(0, feature.semantic_bonus) + kind_bonus + _position_bonus(feature.start, feature.length),
    )


def _feature_score(feature: PatternFeature) -> int:
    return int(round(feature.rarity_bits * RARITY_SCORE_PER_BIT)) + _feature_subjective_bonus(feature)


def _repeated_match_length(value: str, start: int, unit: str) -> int:
    if not unit or start >= len(value):
        return 0
    index = start
    while index < len(value) and value[index] == unit[(index - start) % len(unit)]:
        index += 1
    return index - start


def _same_run_length(value: str, start: int) -> int:
    end = start + 1
    while end < len(value) and value[end] == value[start]:
        end += 1
    return end - start


def _sequence_length(value: str, start: int, step: int) -> int:
    length = 1
    previous = int(value[start], 16)
    for character in value[start + 1:]:
        current = int(character, 16)
        if current != previous + step:
            break
        length += 1
        previous = current
    return length


def _longest_palindromes(value: str, minimum_length: int = 6) -> List[Tuple[int, int]]:
    best_by_start: Dict[int, int] = {}
    n = len(value)
    for center in range(n):
        for left, right in ((center, center), (center, center + 1)):
            while left >= 0 and right < n and value[left] == value[right]:
                length = right - left + 1
                if length >= minimum_length:
                    best_by_start[left] = max(best_by_start.get(left, 0), length)
                left -= 1
                right += 1
    return sorted(best_by_start.items(), key=lambda item: (-item[1], item[0]))[:4]


def _structured_id_features(repeater_id: str) -> List[PatternFeature]:
    features: List[PatternFeature] = []
    if repeater_id[0] == repeater_id[1] == repeater_id[2] and repeater_id[3] == repeater_id[4] == repeater_id[5]:
        features.append(PatternFeature("structured_id", 0, 6, 16.0, 35_000, "AAABBB repeater ID"))
    if repeater_id[0] == repeater_id[1] and repeater_id[2] == repeater_id[3] and repeater_id[4] == repeater_id[5]:
        features.append(PatternFeature("structured_id", 0, 6, 12.0, 30_000, "AABBCC repeater ID"))
    if repeater_id[:2] == repeater_id[2:4] == repeater_id[4:6]:
        features.append(PatternFeature("periodic", 0, 6, 16.0, 45_000, f"two-character unit '{repeater_id[:2]}' repeated"))
    if repeater_id[:3] == repeater_id[3:6]:
        features.append(PatternFeature("periodic", 0, 6, 12.0, 42_000, f"three-character unit '{repeater_id[:3]}' repeated"))
    if repeater_id == repeater_id[::-1]:
        features.append(PatternFeature("palindrome", 0, 6, 12.0, 35_000, "six-character palindromic repeater ID"))
    return features


def _word_chain_length(value: str) -> Tuple[int, Tuple[str, ...]]:
    # Dynamic programming: longest exact prefix that can be segmented into at
    # least two catalog words. There are only 64 positions, so this is cheap.
    best: Dict[int, Tuple[str, ...]] = {0: ()}
    for index in range(len(value)):
        chain = best.get(index)
        if chain is None:
            continue
        for word in WORDS_BY_FIRST.get(value[index], ()):
            end = index + len(word)
            if end <= len(value) and value.startswith(word, index):
                candidate = chain + (word,)
                existing = best.get(end)
                if existing is None or len(candidate) < len(existing):
                    best[end] = candidate
    candidates = [(end, chain) for end, chain in best.items() if len(chain) >= 2]
    return max(candidates, default=(0, ()), key=lambda item: (item[0], -len(item[1])))


def analyze_public_key(public_key_hex: str) -> Dict[str, Any]:
    if len(public_key_hex) != PUBLIC_KEY_HEX_LENGTH or any(c not in HEX_DIGITS for c in public_key_hex):
        raise ValueError("public key must contain exactly 64 uppercase hexadecimal characters")

    features: List[PatternFeature] = []
    repeater_id = public_key_hex[:REPEATER_ID_HEX_LENGTH]

    # Exact preferred/local prefixes. Also recognize arbitrarily long 514
    # repetition and all-4 runs beyond the catalog entries.
    for prefix, (quality, description) in PREFERRED_EXACT_PREFIXES.items():
        if public_key_hex.startswith(prefix):
            features.append(PatternFeature("preferred_exact", 0, len(prefix), 4.0 * len(prefix), quality, description))
    for prefix, (quality, description) in LOCAL_EXACT_PREFIXES.items():
        if public_key_hex.startswith(prefix):
            features.append(PatternFeature("local_exact", 0, len(prefix), 4.0 * len(prefix), quality, description))

    repeated_514 = _repeated_match_length(public_key_hex, 0, "514")
    if repeated_514 >= 6:
        features.append(PatternFeature(
            "local_exact", 0, repeated_514, 4.0 * repeated_514, 300_000,
            f"specific Montreal pattern '514' continues for {repeated_514} characters",
        ))

    all_four_length = _same_run_length(public_key_hex, 0) if public_key_hex[0] == "4" else 0
    if all_four_length >= 4:
        features.append(PatternFeature(
            "specific_run", 0, all_four_length, 4.0 * all_four_length, 250_000,
            f"specific preferred '4' run continues for {all_four_length} characters",
        ))

    # Dictionary words anywhere, plus exact prefix extensions/repetitions.
    prefix_words: List[str] = []
    for word, (quality, description) in HEX_WORDS.items():
        search_start = 0
        while True:
            position = public_key_hex.find(word, search_start)
            if position < 0:
                break
            rarity = max(0.0, 4.0 * len(word) - _location_penalty_bits(len(word), position))
            features.append(PatternFeature("word", position, len(word), rarity, quality, f"vanity word '{word}' ({description})"))
            if position == 0:
                prefix_words.append(word)
            search_start = position + 1

    for word in prefix_words:
        quality, description = HEX_WORDS[word]
        repeated_length = _repeated_match_length(public_key_hex, 0, word)
        if repeated_length >= len(word) * 2:
            features.append(PatternFeature(
                "word_repeat", 0, repeated_length, 4.0 * repeated_length,
                min(300_000, quality + 100_000),
                f"known word '{word}' repeats for {repeated_length} characters ({description})",
            ))

        index = len(word)
        while index < len(public_key_hex) and public_key_hex[index] == word[-1]:
            index += 1
        if index > len(word):
            features.append(PatternFeature(
                "word_extension", 0, index, 4.0 * index,
                min(285_000, quality + 90_000),
                f"word '{word}' extends with {index - len(word)} additional '{word[-1]}' characters",
            ))

    chain_length, chain = _word_chain_length(public_key_hex)
    if chain_length >= 6:
        features.append(PatternFeature(
            "compound_words", 0, chain_length, 4.0 * chain_length,
            min(300_000, 65_000 + sum(HEX_WORDS[word][0] for word in chain) // max(1, len(chain))),
            f"compound vanity prefix {' + '.join(chain)} spans {chain_length} characters",
        ))

    # Specific ascending/descending strings are fixed exact targets; generic
    # sequences below use one free starting nibble and a one-bit direction cost.
    for sequence, label in (("123456789ABCDEF", "ascending"), ("FEDCBA9876543210", "descending")):
        length = 0
        while length < len(sequence) and public_key_hex[length] == sequence[length]:
            length += 1
        if length >= 6:
            features.append(PatternFeature(
                "specific_sequence", 0, length, 4.0 * length, 170_000,
                f"specific {label} hexadecimal sequence continues for {length} characters",
            ))

    # Generic runs and sequences anywhere in all 64 characters.
    index = 0
    while index < len(public_key_hex):
        run_length = _same_run_length(public_key_hex, index)
        if run_length >= 4:
            rarity = max(0.0, 4.0 * (run_length - 1) - _location_penalty_bits(run_length, index))
            features.append(PatternFeature(
                "single_run", index, run_length, rarity, 15_000,
                f"'{public_key_hex[index]}' run of {run_length} characters at offset {index}",
            ))
        index += run_length

    for start in range(len(public_key_hex) - 3):
        for step, label in ((1, "ascending"), (-1, "descending")):
            length = _sequence_length(public_key_hex, start, step)
            if length >= 5:
                rarity = max(0.0, 4.0 * (length - 1) - 1.0 - _location_penalty_bits(length, start))
                features.append(PatternFeature(
                    "sequence", start, length, rarity, 20_000,
                    f"{length}-character {label} sequence at offset {start}",
                ))

    # Generic periodic substrings. Unit lengths up to 12 cover the useful visual
    # patterns without excessive full-score work. The first unit is free; every
    # subsequent matching character contributes four rarity bits.
    for start in range(len(public_key_hex) - 5):
        remaining = len(public_key_hex) - start
        # Unit length one is already represented by the single_run feature.
        for unit_length in range(2, min(12, remaining // 2) + 1):
            unit = public_key_hex[start:start + unit_length]
            repeated_length = _repeated_match_length(public_key_hex, start, unit)
            if repeated_length < max(6, unit_length * 2):
                continue
            constrained = repeated_length - unit_length
            rarity = max(0.0, 4.0 * constrained - _location_penalty_bits(repeated_length, start))
            features.append(PatternFeature(
                "periodic", start, repeated_length, rarity,
                max(0, 50_000 - unit_length * 2_500),
                f"unit '{unit}' repeats for {repeated_length} characters at offset {start}",
            ))

    for start, length in _longest_palindromes(public_key_hex):
        rarity = max(0.0, 4.0 * (length // 2) - _location_penalty_bits(length, start))
        features.append(PatternFeature(
            "palindrome", start, length, rarity, 25_000,
            f"{length}-character palindrome at offset {start}",
        ))

    features.extend(_structured_id_features(repeater_id))

    if not features:
        return {
            "score": 0, "reasons": [], "rarity_bits": 0.0,
            "pattern_length": 0, "pattern_start": 0, "pattern_kind": "none",
            "pattern_family": "none",
        }

    scored = sorted(
        ((_feature_score(feature), feature) for feature in features),
        key=lambda item: (-item[0], -item[1].length, item[1].start, item[1].description),
    )
    primary_score, primary = scored[0]

    # Primary and secondary aesthetic bonuses together are capped below one
    # rarity bit. A pattern with one additional rarity bit therefore always
    # wins, while aesthetics still provide deterministic sub-bit ordering.
    secondary_bonus = 0
    secondary_reasons: List[str] = []
    seen_kinds = {primary.kind}
    for candidate_score, feature in scored[1:]:
        if feature.kind in seen_kinds and feature.start == primary.start:
            continue
        contribution = min(90_000, max(10_000, int(feature.rarity_bits * 1_500)))
        if secondary_bonus + contribution > MAX_SECONDARY_BONUS:
            contribution = max(0, MAX_SECONDARY_BONUS - secondary_bonus)
        if contribution <= 0:
            break
        secondary_bonus += contribution
        seen_kinds.add(feature.kind)
        secondary_reasons.append(feature.description)
        if len(secondary_reasons) >= 5 or secondary_bonus >= MAX_SECONDARY_BONUS:
            break

    final_score = primary_score + secondary_bonus
    reasons = [
        primary.description,
        f"estimated rarity: {primary.rarity_bits:.2f} bits; pattern length {primary.length}/64; offset {primary.start}",
        *secondary_reasons,
    ]
    return {
        "score": int(final_score),
        "reasons": reasons[:8],
        "rarity_bits": round(primary.rarity_bits, 3),
        "pattern_length": int(primary.length),
        "pattern_start": int(primary.start),
        "pattern_kind": primary.kind,
        "pattern_family": pattern_family(primary.kind),
    }


def score_public_key(public_key_hex: str) -> Tuple[int, List[str]]:
    analysis = analyze_public_key(public_key_hex)
    return int(analysis["score"]), list(analysis["reasons"])


def _quick_periodic_candidate(value: str, required_bits: float) -> bool:
    # Every periodic feature contains one of these minimum repeated forms.
    # Returning all such keys is conservative and lets the full scorer decide
    # whether the complete match reaches the current cutoff.
    return _QUICK_PERIODIC_REGEX.search(value) is not None


def quick_candidate(public_key_hex: str, cutoff_score: int) -> bool:
    """Conservative upper-bound filter before the full scorer.

    Every fully-scored feature family is represented here. False positives are
    acceptable; false negatives would permanently hide leaderboard candidates.
    """
    required_bits = max(
        8.0,
        cutoff_score / RARITY_SCORE_PER_BIT
        - MAX_TOTAL_TIEBREAKER_BONUS / RARITY_SCORE_PER_BIT,
    )
    repeater_id = public_key_hex[:6]

    if repeater_id in PREFERRED_EXACT_PREFIXES:
        return True
    for prefix in LOCAL_EXACT_PREFIXES:
        if public_key_hex.startswith(prefix):
            return True

    # Direct structured-ID checks without constructing PatternFeature objects.
    if (
        (repeater_id[0] == repeater_id[1] == repeater_id[2]
         and repeater_id[3] == repeater_id[4] == repeater_id[5])
        or (repeater_id[0] == repeater_id[1]
            and repeater_id[2] == repeater_id[3]
            and repeater_id[4] == repeater_id[5])
        or repeater_id[:2] == repeater_id[2:4] == repeater_id[4:6]
        or repeater_id[:3] == repeater_id[3:6]
        or repeater_id == repeater_id[::-1]
    ):
        return True

    # Prefix word repetitions, final-character extensions, and compounds can
    # be far rarer than the base word. Check them before the ordinary word
    # filter so the fast path remains a true upper-bound for these features.
    prefix_words = [
        word for word in WORDS_BY_FIRST.get(public_key_hex[0], ())
        if public_key_hex.startswith(word)
    ]
    for word in prefix_words:
        quality, _description = HEX_WORDS[word]
        repeated_length = _repeated_match_length(public_key_hex, 0, word)
        if repeated_length >= len(word) * 2:
            feature = PatternFeature(
                "word_repeat", 0, repeated_length, 4.0 * repeated_length,
                min(300_000, quality + 100_000), "quick word repeat",
            )
            if _feature_score(feature) + MAX_SECONDARY_BONUS >= cutoff_score:
                return True
        extension_length = len(word)
        while extension_length < len(public_key_hex) and public_key_hex[extension_length] == word[-1]:
            extension_length += 1
        if extension_length > len(word):
            feature = PatternFeature(
                "word_extension", 0, extension_length, 4.0 * extension_length,
                min(285_000, quality + 90_000), "quick word extension",
            )
            if _feature_score(feature) + MAX_SECONDARY_BONUS >= cutoff_score:
                return True

    if prefix_words:
        chain_length, chain = _word_chain_length(public_key_hex)
        if chain_length >= 6:
            feature = PatternFeature(
                "compound_words", 0, chain_length, 4.0 * chain_length,
                min(300_000, 65_000 + sum(HEX_WORDS[word][0] for word in chain) // len(chain)),
                "quick compound words",
            )
            if _feature_score(feature) + MAX_SECONDARY_BONUS >= cutoff_score:
                return True

    # Prefix words have no location penalty. Later words are admitted only when
    # their adjusted rarity can actually reach the current cutoff.
    for length in WORD_LENGTHS_DESC:
        words = WORD_SETS_BY_LENGTH[length]
        if 4.0 * length >= required_bits and public_key_hex[:length] in words:
            return True

        anywhere_bits = 4.0 * length - math.log2(max(1, PUBLIC_KEY_HEX_LENGTH - length + 1))
        if anywhere_bits < required_bits:
            continue
        match = WORD_REGEX_BY_LENGTH[length].search(public_key_hex, 1)
        if match is not None:
            return True

    if _repeated_match_length(public_key_hex, 0, "514") >= 6:
        return True
    if public_key_hex[0] == "4" and _same_run_length(public_key_hex, 0) >= 4:
        return True

    # One-pass runs and sequences across the key.
    for match in _QUICK_RUN_REGEX.finditer(public_key_hex):
        run_length = len(match.group(0))
        bits = 4.0 * (run_length - 1) - _location_penalty_bits(run_length, match.start())
        if bits >= required_bits:
            return True

    # Minimum five-character sequences are rare enough that admitting all of
    # them is cheaper than calculating every run length in Python.
    if _QUICK_SEQUENCE_REGEX.search(public_key_hex) is not None:
        return True

    if _quick_periodic_candidate(public_key_hex, required_bits):
        return True

    # Every palindrome of length >= 6 contains a central length-6 or length-7
    # palindrome, so these two C-level regex searches are a conservative gate.
    if (
        _QUICK_EVEN_PALINDROME_REGEX.search(public_key_hex) is not None
        or _QUICK_ODD_PALINDROME_REGEX.search(public_key_hex) is not None
    ):
        return True

    return False


# =============================================================================
# LEADERBOARDS
# =============================================================================


def record_sort_key(record: Mapping[str, Any]) -> Tuple[int, int, Tuple[int, ...]]:
    # Higher tuple values are better. Negated nibbles make the lexicographically
    # smaller public key win exact score/length ties, matching sorted_records().
    public_key_tiebreak = tuple(-int(character, 16) for character in str(record["public_key"]))
    return int(record["score"]), int(record.get("pattern_length", 0)), public_key_tiebreak


def sorted_records(records: Iterable[Record]) -> List[Record]:
    return sorted(records, key=lambda record: (-int(record["score"]), -int(record.get("pattern_length", 0)), str(record["public_key"])))


def _leaderboard_cutoff(records: Iterable[Record], limit: int) -> int:
    values = list(records)
    if len(values) < limit:
        return INITIAL_MINIMUM_SCORE
    # Equal-score records may still win on pattern length or public-key order.
    return max(INITIAL_MINIMUM_SCORE, min(int(record["score"]) for record in values))


def empty_category_leaderboards() -> CategoryLeaderboards:
    return {family: {} for family in CATEGORY_BOARD_FAMILIES}


def current_cutoff(
    unique: Mapping[str, Record],
    hall: Mapping[str, Record],
    categories: Optional[Mapping[str, Mapping[str, Record]]] = None,
) -> int:
    cutoffs = [
        _leaderboard_cutoff(unique.values(), TOP_REPEATER_IDS),
        _leaderboard_cutoff(hall.values(), TOP_VANITY_KEYS),
    ]
    if categories:
        cutoffs.extend(
            _leaderboard_cutoff(categories.get(family, {}).values(), TOP_CATEGORY_KEYS)
            for family in CATEGORY_BOARD_FAMILIES
        )
    return min(cutoffs)


def insert_unique(unique: MutableMapping[str, Record], record: Record) -> bool:
    key = str(record["repeater_id"])
    existing = unique.get(key)
    if existing is not None:
        if record_sort_key(record) <= record_sort_key(existing):
            return False
        unique[key] = record
        return True
    if len(unique) < TOP_REPEATER_IDS:
        unique[key] = record
        return True
    worst_key, worst = min(unique.items(), key=lambda item: record_sort_key(item[1]))
    if record_sort_key(record) <= record_sort_key(worst):
        return False
    del unique[worst_key]
    unique[key] = record
    return True


def insert_hall(hall: MutableMapping[str, Record], record: Record) -> bool:
    key = str(record["public_key"])
    existing = hall.get(key)
    if existing is not None:
        if record_sort_key(record) <= record_sort_key(existing):
            return False
        hall[key] = record
        return True
    if len(hall) < TOP_VANITY_KEYS:
        hall[key] = record
        return True
    worst_key, worst = min(hall.items(), key=lambda item: record_sort_key(item[1]))
    if record_sort_key(record) <= record_sort_key(worst):
        return False
    del hall[worst_key]
    hall[key] = record
    return True


def insert_category(categories: CategoryLeaderboards, record: Record) -> Optional[str]:
    family = str(record.get("pattern_family") or pattern_family(str(record.get("pattern_kind", "none"))))
    if family not in CATEGORY_BOARD_FAMILIES:
        return None
    board = categories.setdefault(family, {})
    if insert_hall_with_limit(board, record, TOP_CATEGORY_KEYS):
        return family
    return None


def insert_hall_with_limit(hall: MutableMapping[str, Record], record: Record, limit: int) -> bool:
    key = str(record["public_key"])
    existing = hall.get(key)
    if existing is not None:
        if record_sort_key(record) <= record_sort_key(existing):
            return False
        hall[key] = record
        return True
    if len(hall) < limit:
        hall[key] = record
        return True
    worst_key, worst = min(hall.items(), key=lambda item: record_sort_key(item[1]))
    if record_sort_key(record) <= record_sort_key(worst):
        return False
    del hall[worst_key]
    hall[key] = record
    return True


def make_record_from_material(
    private_key_hex: str,
    seed_hex: Optional[str],
    public_key_hex: str,
    analysis: Mapping[str, Any],
    found_timestamp: float,
    attempts_total: int,
    source: str,
    matched_prefix: Optional[str] = None,
) -> Record:
    repeater_id = public_key_hex[:REPEATER_ID_HEX_LENGTH]
    record: Record = {
        "score": int(analysis["score"]),
        "rarity_bits": float(analysis.get("rarity_bits", 0.0)),
        "pattern_length": int(analysis.get("pattern_length", 0)),
        "pattern_start": int(analysis.get("pattern_start", 0)),
        "pattern_kind": str(analysis.get("pattern_kind", "unknown")),
        "pattern_family": str(analysis.get("pattern_family") or pattern_family(str(analysis.get("pattern_kind", "unknown")))),
        "repeater_id": repeater_id,
        "repeater_id_formatted": ":".join(repeater_id[index:index + 2] for index in range(0, 6, 2)),
        "public_key": public_key_hex,
        "private_key": private_key_hex,
        "ed25519_seed": seed_hex,
        "set_command": f"set prv.key {private_key_hex}",
        "reasons": list(analysis.get("reasons", [])),
        "found_at": datetime.fromtimestamp(found_timestamp, timezone.utc).isoformat().replace("+00:00", "Z"),
        "attempts_total_when_found": int(attempts_total),
        "attempts_counter_scope": "CPU generic attempts snapshot",
        "source": source,
    }
    if matched_prefix:
        record["matched_gpu_prefix"] = matched_prefix
    return record


def make_cpu_record(seed: bytes, public_key_hex: str, analysis: Mapping[str, Any], found_timestamp: float, attempts_total: int) -> Record:
    return make_record_from_material(
        meshcore_private_key_from_seed(seed).hex().upper(),
        seed.hex().upper(),
        public_key_hex,
        analysis,
        found_timestamp,
        attempts_total,
        "cpu_generic",
    )


def validate_record(record: Mapping[str, Any]) -> Optional[Record]:
    public_key_hex = normalize_hex(record.get("public_key"), 64)
    private_key_hex = normalize_hex(record.get("private_key"), 128)
    if public_key_hex is None or private_key_hex is None or public_key_hex.startswith(("00", "FF")):
        return None
    seed_hex = normalize_hex(record.get("ed25519_seed"), 64)
    if seed_hex is not None:
        seed = bytes.fromhex(seed_hex)
        if public_key_from_seed(seed).hex().upper() != public_key_hex:
            return None
        if meshcore_private_key_from_seed(seed).hex().upper() != private_key_hex:
            return None
    else:
        try:
            if public_key_from_meshcore_private(bytes.fromhex(private_key_hex)).hex().upper() != public_key_hex:
                return None
        except (TypeError, ValueError):
            return None

    analysis = analyze_public_key(public_key_hex)
    if int(analysis["score"]) <= 0:
        return None
    validated = make_record_from_material(
        private_key_hex,
        seed_hex,
        public_key_hex,
        analysis,
        time.time(),
        int(record.get("attempts_total_when_found", 0) or 0),
        str(record.get("source") or "restored"),
        str(record.get("matched_gpu_prefix")) if record.get("matched_gpu_prefix") else None,
    )
    validated["found_at"] = record.get("found_at") or validated["found_at"]
    return validated


# =============================================================================
# PERSISTENCE / RECOVERY
# =============================================================================


def _ranked_private(records: Iterable[Record]) -> List[Record]:
    result: List[Record] = []
    for rank, record in enumerate(sorted_records(records), 1):
        item = dict(record)
        item["rank"] = rank
        result.append(item)
    return result


def _public_record(record: Mapping[str, Any], rank: int) -> Dict[str, Any]:
    return {
        "rank": rank,
        "score": record["score"],
        "rarity_bits": record.get("rarity_bits"),
        "pattern_length": record.get("pattern_length"),
        "pattern_start": record.get("pattern_start"),
        "pattern_kind": record.get("pattern_kind"),
        "pattern_family": record.get("pattern_family"),
        "repeater_id": record["repeater_id"],
        "repeater_id_formatted": record["repeater_id_formatted"],
        "public_key": record["public_key"],
        "reasons": record.get("reasons", []),
        "found_at": record.get("found_at"),
        "attempts_total_when_found": record.get("attempts_total_when_found"),
        "source": record.get("source"),
        "matched_gpu_prefix": record.get("matched_gpu_prefix"),
    }


def build_state_payload(
    unique: Mapping[str, Record], hall: Mapping[str, Record], categories: Mapping[str, Mapping[str, Record]], attempts_total: int,
    elapsed_total: float, leaderboard_events: int, recovery_source: str,
    statistics_approximate: bool, private: bool,
) -> Dict[str, Any]:
    unique_sorted = sorted_records(unique.values())
    hall_sorted = sorted_records(hall.values())
    if private:
        unique_records = _ranked_private(unique_sorted)
        hall_records = _ranked_private(hall_sorted)
    else:
        unique_records = [_public_record(record, rank) for rank, record in enumerate(unique_sorted, 1)]
        hall_records = [_public_record(record, rank) for rank, record in enumerate(hall_sorted, 1)]

    category_records: Dict[str, List[Record]] = {}
    for family in CATEGORY_BOARD_FAMILIES:
        records = sorted_records(categories.get(family, {}).values())
        if private:
            category_records[family] = _ranked_private(records)
        else:
            category_records[family] = [
                _public_record(record, rank) for rank, record in enumerate(records, 1)
            ]

    return add_integrity_hash({
        "state_format_version": STATE_FORMAT_VERSION,
        "script_version": SCRIPT_VERSION,
        "score_version": SCORE_VERSION,
        "generated_at": utc_now(),
        "warning": (
            "SECRET: possession of a private key permits use of that MeshCore identity."
            if private else "This file intentionally contains no private keys or seeds."
        ),
        "scoring": {
            "public_key_hex_length": 64,
            "repeater_id_hex_length": 6,
            "rarity_score_per_bit": RARITY_SCORE_PER_BIT,
            "principle": "one additional estimated rarity bit always outranks every aesthetic bonus combined",
            "languages": ["English", "French"],
            "category_families": list(CATEGORY_BOARD_FAMILIES),
        },
        "attempts_total": int(attempts_total),
        "attempts_scope": "generic CPU attempts only; GPU progress is stored separately",
        "elapsed_seconds_total": round(float(elapsed_total), 3),
        "leaderboard_events": int(leaderboard_events),
        "recovery_source": recovery_source,
        "statistics_approximate": bool(statistics_approximate),
        "repeater_ids_top500": unique_records,
        "vanity_hall_of_fame_top500": hall_records,
        "category_leaderboards_top500": category_records,
    })


def checkpoint(
    unique: Mapping[str, Record], hall: Mapping[str, Record], categories: Mapping[str, Mapping[str, Record]], attempts_total: int,
    elapsed_total: float, leaderboard_events: int, recovery_source: str,
    statistics_approximate: bool,
) -> None:
    atomic_write_json(
        PRIVATE_STATE_PATH, PRIVATE_BACKUP_PATH,
        build_state_payload(unique, hall, categories, attempts_total, elapsed_total, leaderboard_events, recovery_source, statistics_approximate, True),
        private=True,
    )
    atomic_write_json(
        PUBLIC_STATE_PATH, PUBLIC_BACKUP_PATH,
        build_state_payload(unique, hall, categories, attempts_total, elapsed_total, leaderboard_events, recovery_source, statistics_approximate, False),
        private=False,
    )
    write_lookup_outputs(unique)


def write_lookup_outputs(unique: Mapping[str, Record]) -> None:
    ranked = sorted_records(unique.values())
    public_lines = []
    for rank, record in enumerate(ranked, 1):
        public_lines.append(json.dumps({
            "rank": rank,
            "repeater_id": record["repeater_id"],
            "public_key": record["public_key"],
            "score": record["score"],
            "rarity_bits": record.get("rarity_bits"),
            "pattern_family": record.get("pattern_family"),
            "pattern_kind": record.get("pattern_kind"),
            "pattern_length": record.get("pattern_length"),
            "reason": (record.get("reasons") or [None])[0],
        }, separators=(",", ":"), sort_keys=False))
    atomic_write_text(
        PUBLIC_ID_LIST_PATH,
        PUBLIC_ID_LIST_BACKUP_PATH,
        "\n".join(public_lines) + ("\n" if public_lines else ""),
        private=False,
    )

    private_map = add_integrity_hash({
        "state_format_version": STATE_FORMAT_VERSION,
        "script_version": SCRIPT_VERSION,
        "generated_at": utc_now(),
        "warning": "SECRET: possession of a private key permits use of that MeshCore identity.",
        "keyed_by": "public_key",
        "keys": {
            str(record["public_key"]): {
                "repeater_id": record["repeater_id"],
                "private_key": record["private_key"],
                "ed25519_seed": record.get("ed25519_seed"),
                "set_command": record["set_command"],
            }
            for record in ranked
        },
    })
    atomic_write_json(
        PRIVATE_KEY_MAP_PATH,
        PRIVATE_KEY_MAP_BACKUP_PATH,
        private_map,
        private=True,
    )


def append_history_event(record: Mapping[str, Any], boards: Sequence[str], attempts_total: int) -> None:
    payload = add_integrity_hash({
        "state_format_version": STATE_FORMAT_VERSION,
        "script_version": SCRIPT_VERSION,
        "score_version": SCORE_VERSION,
        "recorded_at": utc_now(),
        "boards": list(boards),
        "attempts_total": int(attempts_total),
        "record": dict(record),
    })
    descriptor = os.open(str(HISTORY_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        output.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(HISTORY_PATH, 0o600)


def _insert_restored(
    unique: UniqueLeaderboard,
    hall: HallLeaderboard,
    categories: CategoryLeaderboards,
    record: Record,
) -> None:
    insert_unique(unique, record)
    insert_hall(hall, record)
    insert_category(categories, record)


def load_state_file(path: Path) -> Optional[Tuple[UniqueLeaderboard, HallLeaderboard, CategoryLeaderboards, int, float, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not verify_integrity_hash(payload):
        return None

    unique: UniqueLeaderboard = {}
    hall: HallLeaderboard = {}
    categories = empty_category_leaderboards()
    raw_unique = payload.get("repeater_ids_top500", payload.get("repeater_id_top50", []))
    raw_hall = payload.get("vanity_hall_of_fame_top500", payload.get("vanity_hall_of_fame_top50", []))
    if not isinstance(raw_unique, list) or not isinstance(raw_hall, list):
        return None
    raw_categories = payload.get("category_leaderboards_top500", {})
    category_values: List[Any] = []
    if isinstance(raw_categories, dict):
        for family in CATEGORY_BOARD_FAMILIES:
            values = raw_categories.get(family, [])
            if isinstance(values, list):
                category_values.extend(values)
    for raw in [*raw_unique, *raw_hall, *category_values]:
        if isinstance(raw, dict):
            record = validate_record(raw)
            if record is not None:
                _insert_restored(unique, hall, categories, record)
    if (raw_unique or raw_hall) and not unique:
        return None
    try:
        return unique, hall, categories, int(payload.get("attempts_total", 0)), float(payload.get("elapsed_seconds_total", 0.0)), int(payload.get("leaderboard_events", 0))
    except (TypeError, ValueError):
        return None


def recover_from_history() -> Optional[Tuple[UniqueLeaderboard, HallLeaderboard, CategoryLeaderboards, int, float, int]]:
    if not HISTORY_PATH.exists():
        return None
    unique: UniqueLeaderboard = {}
    hall: HallLeaderboard = {}
    categories = empty_category_leaderboards()
    attempts = 0
    events = 0
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as input_file:
            for line in input_file:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or not verify_integrity_hash(payload):
                    continue
                raw = payload.get("record")
                if not isinstance(raw, dict):
                    continue
                record = validate_record(raw)
                if record is None:
                    continue
                _insert_restored(unique, hall, categories, record)
                events += 1
                try:
                    attempts = max(attempts, int(payload.get("attempts_total", 0)))
                except (TypeError, ValueError):
                    pass
    except OSError:
        return None
    return (unique, hall, categories, attempts, 0.0, events) if unique else None


def import_legacy_state() -> Optional[Tuple[UniqueLeaderboard, HallLeaderboard, CategoryLeaderboards, int, float, int, str]]:
    unique: UniqueLeaderboard = {}
    hall: HallLeaderboard = {}
    categories = empty_category_leaderboards()
    attempts = 0
    elapsed = 0.0
    imported: List[str] = []
    for path in LEGACY_PRIVATE_PATHS:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_records = payload.get("keys", [])
        if not isinstance(raw_records, list):
            continue
        loaded_any = False
        for raw in raw_records:
            if isinstance(raw, dict):
                record = validate_record(raw)
                if record is not None:
                    _insert_restored(unique, hall, categories, record)
                    loaded_any = True
        if loaded_any:
            imported.append(str(path))
            try:
                attempts = max(attempts, int(payload.get("attempts_total", 0)))
                elapsed = max(elapsed, float(payload.get("elapsed_seconds_total", 0.0)))
            except (TypeError, ValueError):
                pass
    if not unique:
        return None
    return unique, hall, categories, attempts, elapsed, 0, "legacy_import:" + ",".join(imported)


def restore_state() -> Tuple[UniqueLeaderboard, HallLeaderboard, CategoryLeaderboards, int, float, int, str, bool]:
    for path, source in ((PRIVATE_STATE_PATH, "private_state"), (PRIVATE_BACKUP_PATH, "private_backup")):
        restored = load_state_file(path)
        if restored is not None:
            unique, hall, categories, attempts, elapsed, events = restored
            return unique, hall, categories, attempts, elapsed, events, source, False
    recovered = recover_from_history()
    if recovered is not None:
        unique, hall, categories, attempts, elapsed, events = recovered
        return unique, hall, categories, attempts, elapsed, events, "history_journal", True
    imported = import_legacy_state()
    if imported is not None:
        unique, hall, categories, attempts, elapsed, events, source = imported
        return unique, hall, categories, attempts, elapsed, events, source, True
    return {}, {}, empty_category_leaderboards(), 0, 0.0, 0, "new", False


# =============================================================================
# GPU TARGETS / SCHEDULER
# =============================================================================


def build_gpu_targets() -> Tuple[GpuTarget, ...]:
    targets: Dict[str, Tuple[int, str]] = {}

    def add(prefix: str, priority: int, description: str) -> None:
        prefix = prefix.upper()
        if not GPU_MIN_TARGET_LENGTH <= len(prefix) <= GPU_MAX_TARGET_LENGTH:
            return
        if any(c not in HEX_DIGITS for c in prefix) or prefix.startswith(("00", "FF")):
            return
        existing = targets.get(prefix)
        if existing is None or priority > existing[0]:
            targets[prefix] = (priority, description)

    for prefix, (quality, description) in LOCAL_EXACT_PREFIXES.items():
        if len(prefix) >= GPU_MIN_TARGET_LENGTH:
            add(prefix, 680_000 + quality + len(prefix) * 24_000, description)
        repeated = prefix * ((GPU_MAX_TARGET_LENGTH // len(prefix)) + 2)
        for length in range(GPU_MIN_TARGET_LENGTH, GPU_MAX_TARGET_LENGTH + 1):
            add(repeated[:length], 650_000 + quality + length * 28_000, f"repeated local prefix '{prefix}'")

    # Explicit 514 and all-4 continuations.
    for unit, bonus, label in (("514", 720_000, "Montreal 514 repetition"), ("4", 760_000, "preferred all-4 run")):
        repeated = unit * (GPU_MAX_TARGET_LENGTH + 2)
        for length in range(GPU_MIN_TARGET_LENGTH, GPU_MAX_TARGET_LENGTH + 1):
            add(repeated[:length], bonus + length * 32_000, label)

    for word, (quality, description) in HEX_WORDS.items():
        if len(word) >= GPU_MIN_TARGET_LENGTH:
            add(word, 620_000 + quality + len(word) * 24_000, description)
        if len(word) >= 4:
            for length in range(max(GPU_MIN_TARGET_LENGTH, len(word) + 1), GPU_MAX_TARGET_LENGTH + 1):
                add(word + word[-1] * (length - len(word)), 560_000 + quality + length * 24_000, f"word '{word}' with extended final run")
        if len(word) >= 3:
            repeated = word * ((GPU_MAX_TARGET_LENGTH // len(word)) + 2)
            for length in range(GPU_MIN_TARGET_LENGTH, GPU_MAX_TARGET_LENGTH + 1):
                add(repeated[:length], 650_000 + quality + length * 30_000, f"repeated word/prefix '{word}'")

    strongest = [word for word, _entry in sorted(HEX_WORDS.items(), key=lambda item: (-item[1][0], -len(item[0]), item[0]))[:24]]
    for left in strongest:
        for right in strongest:
            compound = left + right
            if GPU_MIN_TARGET_LENGTH <= len(compound) <= GPU_MAX_TARGET_LENGTH:
                add(compound, 620_000 + HEX_WORDS[left][0] + HEX_WORDS[right][0] + len(compound) * 24_000, f"compound words '{left}' + '{right}'")

    for character in "123456789ABCDE":
        for length in range(GPU_MIN_TARGET_LENGTH, GPU_MAX_TARGET_LENGTH + 1):
            add(character * length, 750_000 + length * 35_000 + (80_000 if character == "4" else 0), f"{length} identical leading '{character}' characters")

    for sequence, label in (("123456789ABCDEF", "ascending"), ("FEDCBA9876543210", "descending")):
        for length in range(GPU_MIN_TARGET_LENGTH, min(GPU_MAX_TARGET_LENGTH, len(sequence)) + 1):
            add(sequence[:length], 640_000 + length * 26_000, f"specific {label} sequence")

    for unit in ("14", "44", "51", "514", "AB", "BA", "ACE", "CA", "FE", "DE", "AD", "BE", "EE", "C0", "C0FFEE", "CAFE", "BEBE", "FACE", "DEAD"):
        repeated = unit * ((GPU_MAX_TARGET_LENGTH // len(unit)) + 2)
        for length in range(GPU_MIN_TARGET_LENGTH, GPU_MAX_TARGET_LENGTH + 1):
            add(repeated[:length], 600_000 + length * 26_000, f"periodic prefix from unit '{unit}'")

    return tuple(
        (prefix, priority, description)
        for prefix, (priority, description) in sorted(targets.items(), key=lambda item: (len(item[0]), -item[1][0], item[0]))
    )


GPU_TARGETS = build_gpu_targets()
GPU_TARGET_BY_PREFIX = {prefix: (prefix, priority, description) for prefix, priority, description in GPU_TARGETS}


def _candidate_gpu_binary_paths() -> List[Path]:
    paths: List[Path] = []
    if os.environ.get("MC_KEYGEN_BINARY"):
        paths.append(Path(os.environ["MC_KEYGEN_BINARY"]).expanduser())
    for candidate in GPU_BINARY_CANDIDATES:
        path = Path(candidate).expanduser()
        paths.append(path if path.is_absolute() else BASE_DIRECTORY / path)
    path_binary = shutil.which("mc-keygen")
    if path_binary:
        paths.append(Path(path_binary))
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


def find_gpu_binary() -> Optional[Path]:
    if not GPU_ENABLED:
        return None
    for path in _candidate_gpu_binary_paths():
        if path.is_file() and os.access(path, os.X_OK):
            return path
    source = BASE_DIRECTORY / GPU_SOURCE_DIRECTORY_NAME
    cargo = shutil.which("cargo")
    if GPU_AUTO_BUILD_IF_SOURCE_PRESENT and cargo and (source / "Cargo.toml").is_file():
        print(f"GPU binary not found; building CUDA backend in {source}...", file=sys.stderr, flush=True)
        completed = subprocess.run([cargo, "build", "--release", "--features", "cuda"], cwd=str(source), check=False)
        candidate = source / "target" / "release" / "mc-keygen"
        if completed.returncode == 0 and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def gpu_subprocess_environment() -> Dict[str, str]:
    environment = os.environ.copy()
    if GPU_DEVICE_INDEX:
        environment["CUDA_VISIBLE_DEVICES"] = GPU_DEVICE_INDEX
    if GPU_CUDA_MODULE_LOADING:
        environment.setdefault("CUDA_MODULE_LOADING", GPU_CUDA_MODULE_LOADING)
    return environment


def _gpu_command(binary: Path, args: Sequence[str]) -> List[str]:
    taskset = shutil.which("taskset")
    if taskset and GPU_HOST_CPU_IDS:
        cpu_list = ",".join(str(cpu_id) for cpu_id in GPU_HOST_CPU_IDS)
        return [taskset, "-c", cpu_list, str(binary), *args]
    return [str(binary), *args]


def load_gpu_progress() -> Dict[str, Any]:
    default = {
        "found_prefixes": set(), "matches_total": 0, "elapsed_seconds_total": 0.0,
        "failures_total": 0, "timeouts_total": 0, "last_error": None,
        "last_match_at": None, "keys_per_second": GPU_DEFAULT_KEYS_PER_SECOND,
        "campaign_runs": {},
    }
    for path in (GPU_PROGRESS_PATH, GPU_PROGRESS_BACKUP_PATH):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not verify_integrity_hash(payload):
            continue
        found = {prefix for prefix in payload.get("found_prefixes", []) if prefix in GPU_TARGET_BY_PREFIX}
        campaign_runs = payload.get("campaign_runs", {})
        if not isinstance(campaign_runs, dict):
            campaign_runs = {}
        default.update({
            "found_prefixes": found,
            "matches_total": int(payload.get("matches_total", 0)),
            "elapsed_seconds_total": float(payload.get("elapsed_seconds_total", 0.0)),
            "failures_total": int(payload.get("failures_total", 0)),
            "timeouts_total": int(payload.get("timeouts_total", 0)),
            "last_error": payload.get("last_error"),
            "last_match_at": payload.get("last_match_at"),
            "keys_per_second": max(1.0, float(payload.get("keys_per_second", GPU_DEFAULT_KEYS_PER_SECOND))),
            "campaign_runs": {str(k): int(v) for k, v in campaign_runs.items()},
        })
        return default
    return default


def save_gpu_progress(progress: Mapping[str, Any]) -> None:
    payload = add_integrity_hash({
        "state_format_version": STATE_FORMAT_VERSION,
        "script_version": SCRIPT_VERSION,
        "generated_at": utc_now(),
        "target_count": len(GPU_TARGETS),
        "found_prefixes": sorted(progress.get("found_prefixes", set())),
        "matches_total": int(progress.get("matches_total", 0)),
        "elapsed_seconds_total": round(float(progress.get("elapsed_seconds_total", 0.0)), 3),
        "failures_total": int(progress.get("failures_total", 0)),
        "timeouts_total": int(progress.get("timeouts_total", 0)),
        "last_error": progress.get("last_error"),
        "last_match_at": progress.get("last_match_at"),
        "keys_per_second": round(float(progress.get("keys_per_second", GPU_DEFAULT_KEYS_PER_SECOND)), 3),
        "campaign_runs": dict(progress.get("campaign_runs", {})),
    })
    atomic_write_json(GPU_PROGRESS_PATH, GPU_PROGRESS_BACKUP_PATH, payload, private=False)


def _campaign_id(campaign: Sequence[str]) -> str:
    return hashlib.sha256("|".join(campaign).encode("ascii")).hexdigest()[:16]


def select_gpu_campaign(progress: Mapping[str, Any]) -> Tuple[str, ...]:
    found = set(progress.get("found_prefixes", set()))
    unresolved = [target for target in GPU_TARGETS if target[0] not in found]
    if not unresolved:
        return ()
    rate = max(1.0, float(progress.get("keys_per_second", GPU_DEFAULT_KEYS_PER_SECOND)))
    runs = progress.get("campaign_runs", {})
    candidates: List[Tuple[int, float, int, int, Tuple[str, ...]]] = []

    for length in sorted({len(target[0]) for target in unresolved}):
        same_length = [target for target in unresolved if len(target[0]) == length]
        same_length.sort(key=lambda target: (-target[1], target[0]))
        for offset in range(0, len(same_length), GPU_MAX_PREFIXES_PER_CAMPAIGN):
            chunk = same_length[offset:offset + GPU_MAX_PREFIXES_PER_CAMPAIGN]
            campaign = tuple(target[0] for target in chunk)
            expected_seconds = (16.0 ** length / max(1, len(campaign))) / rate
            if expected_seconds > GPU_MAX_EXPECTED_CAMPAIGN_SECONDS:
                continue
            campaign_runs = int(runs.get(_campaign_id(campaign), 0))
            priority = sum(target[1] for target in chunk)
            candidates.append((campaign_runs, expected_seconds, -priority, length, campaign))

    if not candidates:
        return ()
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return candidates[0][4]


def mark_gpu_targets_found(found: Set[str], public_key_hex: str) -> List[str]:
    newly: List[str] = []
    for prefix, _priority, _description in GPU_TARGETS:
        if prefix not in found and public_key_hex.startswith(prefix):
            found.add(prefix)
            newly.append(prefix)
    return newly


def _recursive_find_value(payload: Any, names: Sequence[str]) -> Any:
    normalized = {name.lower() for name in names}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in normalized:
                return value
        for value in payload.values():
            found = _recursive_find_value(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _recursive_find_value(item, names)
            if found is not None:
                return found
    return None


def _parse_json_output(text: str) -> Any:
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


def _loose_hex(value: Any) -> Optional[str]:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        return bytes(value).hex().upper()
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized.startswith("0X"):
        normalized = normalized[2:]
    normalized = "".join(c for c in normalized if not c.isspace())
    return normalized if normalized and all(c in HEX_DIGITS for c in normalized) else None


def normalize_external_private_key(payload: Any, public_key_hex: str) -> Tuple[str, Optional[str]]:
    seed_hex = _loose_hex(_recursive_find_value(payload, ("seed", "ed25519_seed", "private_seed", "secret_seed")))
    if seed_hex is not None and len(seed_hex) == 64:
        seed = bytes.fromhex(seed_hex)
        if public_key_from_seed(seed).hex().upper() == public_key_hex:
            return meshcore_private_key_from_seed(seed).hex().upper(), seed_hex

    private_hex = _loose_hex(_recursive_find_value(payload, (
        "private_key", "private_key_hex", "private", "secret_key", "secret_key_hex",
        "secret", "prv_key", "prv_key_hex", "prv",
    )))
    if private_hex is None:
        raise ValueError("mc-keygen JSON did not contain a private key")
    if len(private_hex) == 64:
        seed = bytes.fromhex(private_hex)
        if public_key_from_seed(seed).hex().upper() != public_key_hex:
            raise ValueError("GPU seed does not derive public key")
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


def parse_gpu_result(stdout_text: str, campaign: Sequence[str]) -> Dict[str, Any]:
    payload = _parse_json_output(stdout_text)
    public_key_hex = _loose_hex(_recursive_find_value(payload, ("public_key", "public_key_hex", "public", "pubkey", "address")))
    if public_key_hex is None or len(public_key_hex) != 64 or public_key_hex.startswith(("00", "FF")):
        raise ValueError("mc-keygen returned an invalid public key")
    matched_prefix = _loose_hex(_recursive_find_value(payload, ("matched_prefix", "matched", "prefix", "match")))
    if matched_prefix not in campaign or not public_key_hex.startswith(matched_prefix or ""):
        matched_prefix = next((prefix for prefix in campaign if public_key_hex.startswith(prefix)), None)
    if matched_prefix is None:
        raise ValueError("GPU result does not match active campaign")

    private_key_hex, seed_hex = normalize_external_private_key(payload, public_key_hex)
    analysis = analyze_public_key(public_key_hex)
    target = GPU_TARGET_BY_PREFIX.get(matched_prefix)
    if target is not None:
        analysis = dict(analysis)
        analysis["reasons"] = [f"GPU exact prefix '{matched_prefix}' ({target[2]})", *analysis["reasons"]][:8]

    raw_attempts = _recursive_find_value(payload, ("attempts", "keys_checked", "checked"))
    raw_elapsed = _recursive_find_value(payload, ("elapsed_secs", "elapsed_seconds", "elapsed"))
    try:
        attempts = int(raw_attempts) if raw_attempts is not None else None
    except (TypeError, ValueError):
        attempts = None
    try:
        elapsed = float(raw_elapsed) if raw_elapsed is not None else None
    except (TypeError, ValueError):
        elapsed = None
    return {
        "private_key_hex": private_key_hex, "seed_hex": seed_hex,
        "public_key_hex": public_key_hex, "analysis": analysis,
        "matched_prefix": matched_prefix, "gpu_attempts": attempts,
        "gpu_reported_elapsed_seconds": elapsed,
    }


class GpuEngine:
    def __init__(self, binary: Path, output_queue: "queue.Queue[Dict[str, Any]]") -> None:
        self.binary = binary
        self.output_queue = output_queue
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.progress = load_gpu_progress()
        self.current_process: Optional[subprocess.Popen] = None
        self.active_campaign: Tuple[str, ...] = ()
        self.active_started: Optional[float] = None
        self.thread = threading.Thread(target=self._run, name="meshcore-gpu", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            process = self.current_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.thread.join(timeout=8.0)
        with self.lock:
            process = self.current_process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        self.thread.join(timeout=2.0)
        self.save()

    def save(self) -> None:
        with self.lock:
            snapshot = dict(self.progress)
            snapshot["found_prefixes"] = set(self.progress["found_prefixes"])
            snapshot["campaign_runs"] = dict(self.progress["campaign_runs"])
        save_gpu_progress(snapshot)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            snapshot = dict(self.progress)
            snapshot["found_prefixes"] = set(self.progress["found_prefixes"])
            snapshot["campaign_runs"] = dict(self.progress["campaign_runs"])
            snapshot["active_campaign"] = self.active_campaign
            snapshot["active_started"] = self.active_started
            snapshot["alive"] = self.thread.is_alive()
        return snapshot

    def register_public_key(self, public_key_hex: str) -> None:
        with self.lock:
            changed = bool(mark_gpu_targets_found(self.progress["found_prefixes"], public_key_hex))
        if changed:
            self.save()

    def _verify_backend(self) -> bool:
        if not GPU_RUN_STARTUP_SELF_TEST:
            return True
        command = _gpu_command(self.binary, ["A", "--gpu-only", "--verify"])
        try:
            completed = subprocess.run(
                command, cwd=str(BASE_DIRECTORY), env=gpu_subprocess_environment(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=GPU_SELF_TEST_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._record_failure(f"GPU startup verification could not run: {error}")
            return False
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
        if completed.returncode != 0:
            self._record_failure("GPU startup verification failed: " + (output[-1000:] if output else str(completed.returncode)))
            return False
        self.output_queue.put({"type": "gpu_status", "message": "mc-keygen GPU startup verification passed"})
        return True

    def _run(self) -> None:
        if not self._verify_backend():
            self.output_queue.put({"type": "gpu_disabled", "message": "GPU disabled because startup verification failed"})
            return
        consecutive_failures = 0
        while not self.stop_event.is_set():
            with self.lock:
                campaign = select_gpu_campaign(self.progress)
                self.active_campaign = campaign
                self.active_started = time.monotonic() if campaign else None
            if not campaign:
                self.output_queue.put({"type": "gpu_status", "message": "no GPU campaign is currently feasible or all targets are complete"})
                self.stop_event.wait(60.0)
                continue

            campaign_id = _campaign_id(campaign)
            command = _gpu_command(self.binary, [*campaign, "--json", "--gpu-only"])
            started = time.monotonic()
            timed_out = False
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
                try:
                    process = subprocess.Popen(
                        command, cwd=str(BASE_DIRECTORY), env=gpu_subprocess_environment(),
                        stdout=stdout_file, stderr=stderr_file,
                    )
                except OSError as error:
                    self._record_failure(f"could not start mc-keygen: {error}")
                    break
                with self.lock:
                    self.current_process = process
                    self.progress["campaign_runs"][campaign_id] = int(self.progress["campaign_runs"].get(campaign_id, 0)) + 1

                deadline = started + GPU_CAMPAIGN_TIME_SLICE_SECONDS
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
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout_text = stdout_file.read().decode("utf-8", "replace")
                stderr_text = stderr_file.read().decode("utf-8", "replace")
                with self.lock:
                    self.current_process = None
                    self.active_started = None
                    self.progress["elapsed_seconds_total"] += elapsed

            if self.stop_event.is_set():
                break
            if timed_out:
                with self.lock:
                    self.progress["timeouts_total"] += 1
                self.save()
                self.output_queue.put({
                    "type": "gpu_status",
                    "message": f"rotating campaign len={len(campaign[0])}, n={len(campaign)} after {elapsed:.0f}s time slice",
                })
                continue
            if return_code != 0:
                message = stderr_text.strip() or stdout_text.strip() or f"exit code {return_code}"
                self._record_failure(f"mc-keygen failed: {message[-1000:]}")
                consecutive_failures += 1
                if consecutive_failures >= GPU_MAX_CONSECUTIVE_FAILURES:
                    self.output_queue.put({"type": "gpu_disabled", "message": "GPU disabled after repeated failures"})
                    break
                self.stop_event.wait(GPU_RETRY_DELAY_SECONDS)
                continue

            try:
                parsed = parse_gpu_result(stdout_text, campaign)
            except Exception as error:
                self._record_failure(f"could not validate mc-keygen result: {error}")
                consecutive_failures += 1
                if consecutive_failures >= GPU_MAX_CONSECUTIVE_FAILURES:
                    self.output_queue.put({"type": "gpu_disabled", "message": "GPU disabled after repeated invalid results"})
                    break
                continue

            consecutive_failures = 0
            attempts = parsed.get("gpu_attempts")
            reported_elapsed = parsed.get("gpu_reported_elapsed_seconds")
            measured_rate: Optional[float] = None
            if isinstance(attempts, int) and attempts > 0:
                elapsed_for_rate = float(reported_elapsed) if isinstance(reported_elapsed, (int, float)) and reported_elapsed > 0 else elapsed
                if elapsed_for_rate > 0:
                    measured_rate = attempts / elapsed_for_rate

            with self.lock:
                newly = mark_gpu_targets_found(self.progress["found_prefixes"], parsed["public_key_hex"])
                self.progress["matches_total"] += 1
                self.progress["last_error"] = None
                self.progress["last_match_at"] = utc_now()
                if measured_rate is not None:
                    old_rate = float(self.progress.get("keys_per_second", GPU_DEFAULT_KEYS_PER_SECOND))
                    self.progress["keys_per_second"] = old_rate * (1.0 - GPU_RATE_EWMA_ALPHA) + measured_rate * GPU_RATE_EWMA_ALPHA
            self.save()
            parsed.update({
                "type": "gpu_result", "found_timestamp": time.time(),
                "gpu_elapsed_seconds": elapsed, "newly_completed_targets": newly,
                "gpu_measured_keys_per_second": measured_rate,
            })
            self.output_queue.put(parsed)

        with self.lock:
            self.active_campaign = ()
            self.active_started = None
            self.current_process = None
        self.save()

    def _record_failure(self, message: str) -> None:
        with self.lock:
            self.progress["failures_total"] += 1
            self.progress["last_error"] = message
        self.save()
        self.output_queue.put({"type": "gpu_error", "message": message})


# =============================================================================
# CPU WORKERS
# =============================================================================


def add_to_shared_counter(counter, amount: int) -> None:
    with counter.get_lock():
        counter.value += amount


def worker_main(worker_index, stop_event, result_queue, counter, cutoff) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        result_queue.cancel_join_thread()
    except (AttributeError, OSError):
        pass
    if CPU_NICE_INCREMENT:
        try:
            os.nice(CPU_NICE_INCREMENT)
        except OSError:
            pass
    if CPU_PIN_WORKERS:
        try:
            os.sched_setaffinity(0, {CPU_WORKER_CPU_IDS[worker_index % len(CPU_WORKER_CPU_IDS)]})
        except OSError:
            pass

    local_count = 0
    local_cutoff = int(cutoff.value)
    refresh_countdown = 1_024
    try:
        while not stop_event.is_set():
            random_block = os.urandom(32 * CPU_RANDOM_SEED_BATCH)
            for offset in range(0, len(random_block), 32):
                if stop_event.is_set():
                    break
                seed = random_block[offset:offset + 32]
                public_key = public_key_from_seed(seed)
                local_count += 1
                if public_key[0] not in (0x00, 0xFF):
                    public_key_hex = public_key.hex().upper()
                    if quick_candidate(public_key_hex, local_cutoff):
                        analysis = analyze_public_key(public_key_hex)
                        if int(analysis["score"]) >= local_cutoff:
                            result = (seed, public_key_hex, analysis, time.time())
                            while not stop_event.is_set():
                                try:
                                    result_queue.put(result, timeout=0.25)
                                    break
                                except queue.Full:
                                    continue

                refresh_countdown -= 1
                if refresh_countdown <= 0:
                    local_cutoff = int(cutoff.value)
                    refresh_countdown = 1_024
                if local_count >= COUNTER_BATCH_SIZE:
                    add_to_shared_counter(counter, local_count)
                    local_count = 0
    except KeyboardInterrupt:
        pass
    finally:
        if local_count:
            add_to_shared_counter(counter, local_count)


def start_worker(context, index, stop_event, result_queue, counter, cutoff):
    process = context.Process(target=worker_main, args=(index, stop_event, result_queue, counter, cutoff), daemon=True)
    process.start()
    return process


# =============================================================================
# VALIDATION / SELF TESTS
# =============================================================================


def validate_configuration() -> None:
    if CPU_WORKERS < 1 or TOP_REPEATER_IDS < 1 or TOP_VANITY_KEYS < 1 or TOP_CATEGORY_KEYS < 1:
        raise ValueError("worker and leaderboard sizes must be positive")
    if MAX_TOTAL_TIEBREAKER_BONUS >= RARITY_SCORE_PER_BIT:
        raise ValueError("all aesthetic bonuses combined must remain below one rarity bit")
    if not 6 <= GPU_MIN_TARGET_LENGTH <= GPU_MAX_TARGET_LENGTH <= 62:
        raise ValueError("invalid GPU target length range")
    for prefix in [*PREFERRED_EXACT_PREFIXES, *LOCAL_EXACT_PREFIXES]:
        if any(c not in HEX_DIGITS for c in prefix) or prefix.startswith(("00", "FF")):
            raise ValueError(f"invalid preferred prefix {prefix}")
    if any(prefix.startswith(("00", "FF")) for prefix, _priority, _description in GPU_TARGETS):
        raise ValueError("GPU target catalog contains a reserved prefix")


def run_self_tests() -> None:
    seed = bytes.fromhex("9D61B19DEFFD5A60BA844AF492EC2CC44449C5697B326919703BAC031CAE7F60")
    expected_public = "D75A980182B10AB7D54BFED3C964073A0EE172F3DAA62325AF021A68F707511A"
    if public_key_from_seed(seed).hex().upper() != expected_public:
        raise RuntimeError("Ed25519 public-key self-test failed")
    expanded = meshcore_private_key_from_seed(seed)
    if public_key_from_meshcore_private(expanded).hex().upper() != expected_public:
        raise RuntimeError("MeshCore expanded-private-key self-test failed")

    def padded(prefix: str) -> str:
        filler = hashlib.sha256((prefix + "|self-test").encode("ascii")).hexdigest().upper()
        return (prefix + filler)[:64]

    samples = {
        "random": padded("C0FFEF"),
        "coffee": padded("C0FFEE"),
        "coffee2": padded("C0FFEEC0FFEE"),
        "coffee3": padded("C0FFEEC0FFEEC0FFEE"),
        "coffee_tail": padded("C0FFEEEEEEEEEEEE"),
        "514_2": padded("514514"),
        "514_6": padded("514514514514514514"),
        "fours": padded("444444444444"),
        "ones_long": padded("11111111111111111"),
        "bad_repeat": padded("BADBADBADBADBAD"),
        "ace_repeat": padded("ACEACEACEACEACEACE"),
        "sequence": padded("123456789ABCDEF"),
        "french": padded("EFFACEE"),
    }
    scores = {name: analyze_public_key(value)["score"] for name, value in samples.items()}
    if not scores["coffee"] > scores["random"]:
        raise RuntimeError("word scoring self-test failed")
    if not scores["coffee3"] > scores["coffee2"] > scores["coffee"]:
        raise RuntimeError("repeated-word length scoring self-test failed")
    if not scores["coffee_tail"] > scores["coffee"]:
        raise RuntimeError("word-tail scoring self-test failed")
    if not scores["514_6"] > scores["514_2"]:
        raise RuntimeError("514 repetition scoring self-test failed")
    if not scores["fours"] > scores["coffee"]:
        raise RuntimeError("long run scoring self-test failed")
    if not scores["ones_long"] > scores["bad_repeat"]:
        raise RuntimeError("long single-run priority self-test failed")
    if not scores["ace_repeat"] > scores["bad_repeat"]:
        raise RuntimeError("long repeated-word priority self-test failed")
    if scores["french"] <= scores["coffee"]:
        raise RuntimeError("French word scoring self-test failed")
    if scores["sequence"] <= scores["fours"]:
        raise RuntimeError("sequence scoring self-test failed")
    if MAX_TOTAL_TIEBREAKER_BONUS >= RARITY_SCORE_PER_BIT:
        raise RuntimeError("rarity dominance invariant failed")

    for name in ("coffee2", "coffee_tail", "ace_repeat"):
        value = samples[name]
        if not quick_candidate(value, int(analyze_public_key(value)["score"])):
            raise RuntimeError(f"quick filter rejected qualifying {name} pattern")


# =============================================================================
# MAIN
# =============================================================================


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously harvest and rank MeshCore vanity identities.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    parser.add_argument("--self-test", action="store_true", help="run validation tests and exit")
    parser.add_argument("--no-gpu", action="store_true", help="disable the mc-keygen GPU backend")
    parser.add_argument("--cpu-workers", type=int, metavar="N", help="number of generic CPU workers")
    parser.add_argument("--max-runtime", type=float, default=None, metavar="SECONDS", help="stop cleanly after this many seconds")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ["MESHCORE_VANITY_DATA_DIR"]) if os.environ.get("MESHCORE_VANITY_DATA_DIR") else None,
        help="state directory (or set MESHCORE_VANITY_DATA_DIR)",
    )
    return parser.parse_args(argv)


def apply_runtime_arguments(arguments: argparse.Namespace) -> None:
    global GPU_ENABLED, CPU_WORKERS, CPU_WORKER_CPU_IDS, GPU_HOST_CPU_IDS, MAX_RUNTIME_SECONDS
    if arguments.data_dir is not None:
        set_data_directory(arguments.data_dir)
    if arguments.no_gpu:
        GPU_ENABLED = False
    if arguments.cpu_workers is not None:
        if arguments.cpu_workers < 1:
            raise ValueError("--cpu-workers must be positive")
        CPU_WORKERS = min(arguments.cpu_workers, len(AVAILABLE_CPU_IDS))
        CPU_WORKER_CPU_IDS = AVAILABLE_CPU_IDS[:CPU_WORKERS]
        GPU_HOST_CPU_IDS = AVAILABLE_CPU_IDS[CPU_WORKERS:] or AVAILABLE_CPU_IDS[-1:]
    if arguments.max_runtime is not None:
        if arguments.max_runtime <= 0:
            raise ValueError("--max-runtime must be positive")
        MAX_RUNTIME_SECONDS = arguments.max_runtime


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parse_arguments(argv)
    try:
        apply_runtime_arguments(arguments)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    validate_configuration()
    run_self_tests()
    if arguments.self_test:
        print(
            f"Self-test passed: {len(HEX_WORDS)} English/French words, "
            f"{len(GPU_TARGETS)} GPU targets, score version {SCORE_VERSION}."
        )
        return 0
    lock_file = acquire_single_instance_lock()

    unique, hall, categories, attempts_before, elapsed_before, leaderboard_events, recovery_source, statistics_approximate = restore_state()
    cutoff_value = current_cutoff(unique, hall, categories)

    context = mp.get_context("spawn")
    stop_event = context.Event()
    result_queue = context.Queue(maxsize=RESULT_QUEUE_SIZE)
    counter = context.Value("Q", 0)
    cutoff = context.Value("Q", cutoff_value)

    gpu_output_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    gpu_binary = find_gpu_binary()
    gpu_engine: Optional[GpuEngine] = None
    if gpu_binary is not None:
        gpu_engine = GpuEngine(gpu_binary, gpu_output_queue)
        known_records = {record["public_key"]: record for record in [*hall.values(), *(record for board in categories.values() for record in board.values())]}
        for record in known_records.values():
            gpu_engine.register_public_key(str(record["public_key"]))

    shutdown_requested = False

    def request_shutdown(_signum=None, _frame=None) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        stop_event.set()
        if gpu_engine is not None:
            gpu_engine.stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    try:
        signal.signal(signal.SIGTERM, request_shutdown)
    except (AttributeError, OSError):
        pass

    workers = [start_worker(context, index, stop_event, result_queue, counter, cutoff) for index in range(CPU_WORKERS)]
    restart_times: Deque[float] = deque()
    if gpu_engine is not None:
        gpu_engine.start()

    start_monotonic = time.monotonic()
    last_status = start_monotonic
    last_checkpoint = start_monotonic
    last_urgent_checkpoint = 0.0
    last_health_check = start_monotonic
    changed = bool(unique or hall)

    print(f"Script:                 {Path(__file__).name}")
    print(f"Version:                {SCRIPT_VERSION}")
    print(f"CPU workers:            {CPU_WORKERS} (reserving {RESERVED_LOGICAL_CPUS} logical CPUs)")
    print(f"CPU seed batch:         {CPU_RANDOM_SEED_BATCH}")
    print(f"GPU backend:            {gpu_binary if gpu_binary is not None else 'not available; CPU-only'}")
    print(f"GPU targets:            {len(GPU_TARGETS)} exact prefixes")
    print(f"GPU target length:      {GPU_MIN_TARGET_LENGTH}-{GPU_MAX_TARGET_LENGTH}")
    print(f"GPU campaign time slice:{GPU_CAMPAIGN_TIME_SLICE_SECONDS:.0f}s")
    print(f"Repeater leaderboard:   top {TOP_REPEATER_IDS}, one key per first-six ID")
    print(f"Vanity hall of fame:    top {TOP_VANITY_KEYS}, unrestricted")
    print(f"Category leaderboards:  top {TOP_CATEGORY_KEYS} each ({', '.join(CATEGORY_BOARD_FAMILIES)})")
    print(f"Scoring span:           all 64 hexadecimal public-key characters")
    print(f"Vanity dictionary:      {len(HEX_WORDS)} English/French encodings")
    print(f"Initial cutoff:         {cutoff_value}")
    print(f"Recovery source:        {recovery_source}")
    print(f"Resumed unique/hall:    {len(unique)}/{len(hall)}")
    print(f"Resumed categories:     " + ", ".join(f"{family}={len(categories[family])}" for family in CATEGORY_BOARD_FAMILIES))
    print(f"Data directory:         {DATA_DIRECTORY}")
    print(f"Public output:          {PUBLIC_STATE_PATH.name}")
    print(f"Private output:         {PRIVATE_STATE_PATH.name} (mode 0600)")
    print("Searching; press Ctrl+C to stop...", flush=True)

    if unique or hall:
        checkpoint(unique, hall, categories, attempts_before, elapsed_before, leaderboard_events, recovery_source, statistics_approximate)
        changed = False
        last_checkpoint = time.monotonic()

    def current_cpu_attempts() -> int:
        with counter.get_lock():
            return int(counter.value)

    def accept_record(record: Record, now: float) -> None:
        nonlocal leaderboard_events, changed, last_checkpoint, last_urgent_checkpoint
        boards: List[str] = []
        if insert_unique(unique, record):
            boards.append("repeater_ids_top500")
        if insert_hall(hall, record):
            boards.append("vanity_hall_of_fame_top500")
        category = insert_category(categories, record)
        if category is not None:
            boards.append(f"{category}_top500")
        if not boards:
            return
        leaderboard_events += 1
        changed = True
        attempts_total = attempts_before + current_cpu_attempts()
        append_history_event(record, boards, attempts_total)
        cutoff.value = current_cutoff(unique, hall, categories)
        if int(record["score"]) >= URGENT_CHECKPOINT_SCORE and now - last_urgent_checkpoint >= URGENT_CHECKPOINT_MIN_INTERVAL_SECONDS:
            checkpoint(unique, hall, categories, attempts_total, elapsed_before + (now - start_monotonic), leaderboard_events, recovery_source, statistics_approximate)
            changed = False
            last_checkpoint = now
            last_urgent_checkpoint = now

    exit_code = 0
    try:
        while not shutdown_requested:
            now = time.monotonic()
            elapsed_this_run = now - start_monotonic
            if MAX_RUNTIME_SECONDS and elapsed_this_run >= MAX_RUNTIME_SECONDS:
                request_shutdown()
                break

            try:
                seed, public_key_hex, analysis, found_timestamp = result_queue.get(timeout=0.15)
                record = make_cpu_record(seed, public_key_hex, analysis, float(found_timestamp), attempts_before + current_cpu_attempts())
                accept_record(record, time.monotonic())
                if gpu_engine is not None:
                    gpu_engine.register_public_key(public_key_hex)
            except queue.Empty:
                pass

            for _ in range(32):
                try:
                    message = gpu_output_queue.get_nowait()
                except queue.Empty:
                    break
                message_type = message.get("type")
                if message_type == "gpu_result":
                    record = make_record_from_material(
                        str(message["private_key_hex"]), message.get("seed_hex"), str(message["public_key_hex"]),
                        message["analysis"], float(message["found_timestamp"]), attempts_before + current_cpu_attempts(),
                        "nvidia_gpu_mc_keygen", str(message["matched_prefix"]),
                    )
                    record["gpu_campaign_elapsed_seconds"] = round(float(message.get("gpu_elapsed_seconds", 0.0)), 3)
                    record["gpu_targets_completed_by_result"] = list(message.get("newly_completed_targets", []))
                    accept_record(record, time.monotonic())
                    rate = message.get("gpu_measured_keys_per_second")
                    rate_text = f" | {rate:,.0f} GPU keys/s" if isinstance(rate, (int, float)) and rate > 0 else ""
                    print(
                        f"GPU match: {record['public_key']} via {record['matched_gpu_prefix']} | "
                        f"{record['rarity_bits']:.2f} bits | score {record['score']}{rate_text}",
                        file=sys.stderr, flush=True,
                    )
                elif message_type in ("gpu_error", "gpu_disabled", "gpu_status"):
                    print(f"GPU: {message.get('message', message_type)}", file=sys.stderr, flush=True)

            now = time.monotonic()
            elapsed_this_run = now - start_monotonic
            if now - last_status >= STATUS_INTERVAL_SECONDS:
                attempts_run = current_cpu_attempts()
                rate = attempts_run / elapsed_this_run if elapsed_this_run else 0.0
                best = sorted_records(hall.values())[0] if hall else None
                gpu_summary = "off"
                if gpu_engine is not None:
                    snapshot = gpu_engine.snapshot()
                    active = snapshot.get("active_campaign", ())
                    if active:
                        active_elapsed = now - float(snapshot.get("active_started") or now)
                        active_text = f"len={len(active[0])},n={len(active)},elapsed={active_elapsed:.0f}s"
                    else:
                        active_text = "idle"
                    gpu_summary = (
                        f"{len(snapshot['found_prefixes'])}/{len(GPU_TARGETS)} targets, "
                        f"{snapshot['matches_total']} matches, {snapshot['keys_per_second']:,.0f} est keys/s, {active_text}"
                    )
                best_text = f"{best['score']} ({best['rarity_bits']:.2f} bits, len {best['pattern_length']})" if best else "0"
                print(
                    f"CPU attempts: {attempts_before + attempts_run:,} | CPU rate: {rate:,.0f}/s | "
                    f"Unique/Hall: {len(unique)}/{len(hall)} | Cutoff: {int(cutoff.value)} | "
                    f"Best: {best_text} | GPU: {gpu_summary}",
                    file=sys.stderr, flush=True,
                )
                last_status = now

            if changed and now - last_checkpoint >= CHECKPOINT_INTERVAL_SECONDS:
                checkpoint(unique, hall, categories, attempts_before + current_cpu_attempts(), elapsed_before + elapsed_this_run, leaderboard_events, recovery_source, statistics_approximate)
                changed = False
                last_checkpoint = now

            if now - last_health_check >= WORKER_HEALTH_INTERVAL_SECONDS:
                for index, process in enumerate(workers):
                    if process.is_alive() or shutdown_requested:
                        continue
                    restart_now = time.monotonic()
                    restart_times.append(restart_now)
                    while restart_times and restart_now - restart_times[0] > WORKER_RESTART_WINDOW_SECONDS:
                        restart_times.popleft()
                    if len(restart_times) > MAX_WORKER_RESTARTS:
                        raise RuntimeError("CPU workers repeatedly exited")
                    workers[index] = start_worker(context, index, stop_event, result_queue, counter, cutoff)
                    print(f"Restarted CPU worker {index + 1}.", file=sys.stderr, flush=True)
                last_health_check = now

    except KeyboardInterrupt:
        request_shutdown()
        exit_code = 130
    except Exception as error:
        request_shutdown()
        print(f"Fatal error: {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        stop_event.set()
        if gpu_engine is not None:
            gpu_engine.stop()
        for process in workers:
            process.join(timeout=5.0)
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)

        attempts_run = current_cpu_attempts()
        elapsed_run = time.monotonic() - start_monotonic
        attempts_total = attempts_before + attempts_run
        elapsed_total = elapsed_before + elapsed_run
        checkpoint(unique, hall, categories, attempts_total, elapsed_total, leaderboard_events, recovery_source, statistics_approximate)
        try:
            lock_file.close()
        except OSError:
            pass

    print(f"CPU attempts run:       {attempts_run:,}")
    print(f"CPU attempts total:     {attempts_total:,}")
    print(f"Elapsed this run:       {elapsed_run:.2f}s")
    print(f"Elapsed total:          {elapsed_total:.2f}s")
    if gpu_engine is not None:
        snapshot = gpu_engine.snapshot()
        print(f"GPU matches total:      {snapshot['matches_total']}")
        print(f"GPU targets satisfied:  {len(snapshot['found_prefixes'])}/{len(GPU_TARGETS)}")
        print(f"GPU estimated rate:     {snapshot['keys_per_second']:,.0f} keys/s")
    hall_sorted = sorted_records(hall.values())
    if hall_sorted:
        best = hall_sorted[0]
        print(f"Best score:             {best['score']}")
        print(f"Best rarity:            {best['rarity_bits']:.2f} bits")
        print(f"Best pattern length:    {best['pattern_length']}/64")
        print(f"Best repeater ID:       {best['repeater_id']}")
        print(f"Best public key:        {best['public_key']}")
        print(f"Reason:                 {', '.join(best.get('reasons', []))}")
    print(f"Saved private:          {PRIVATE_STATE_PATH}")
    print(f"Saved public:           {PUBLIC_STATE_PATH}")
    print(f"Public ID index:        {PUBLIC_ID_LIST_PATH}")
    print(f"Private key map:        {PRIVATE_KEY_MAP_PATH}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
