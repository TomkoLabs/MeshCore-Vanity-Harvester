"""Record construction, ranking and board insertion.

Five complementary views are kept:

* one best identity per six-character repeater ID;
* an unrestricted hall of fame;
* one board each for the word, single-run and periodic families.

The hall of fame additionally enforces a *diversity* rule. Rarity alone would
happily fill five hundred slots with five hundred variations of the same shape,
which is the opposite of interesting. Each record carries a canonical pattern
signature (the repeated word, the run digit, the periodic unit), and the hall
keeps only the best few entries sharing one signature. A better key still
displaces the worst of its own signature group, so the rule costs nothing in
quality while making the board show genuine variety.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import (AbstractSet, Any, Dict, Iterable, List, Mapping, MutableMapping,
                    Optional, Sequence, Tuple)

from . import SCORE_VERSION
from .config import BoardConfig, PUBLIC_KEY_HEX_LENGTH, REPEATER_ID_HEX_LENGTH
from .keys import (
    is_reserved_public_key,
    meshcore_private_key_from_seed,
    normalize_hex,
    public_key_from_meshcore_private,
    public_key_from_seed,
)
from .scoring import analyze_public_key, pattern_family

Record = Dict[str, Any]
Board = Dict[str, Record]
CategoryBoards = Dict[str, Board]


def record_sort_key(record: Mapping[str, Any]) -> Tuple[int, int, Tuple[int, ...]]:
    """Higher is better. Ties break on pattern length, then lexicographic key."""
    tiebreak = tuple(-int(character, 16) for character in str(record["public_key"]))
    return int(record["score"]), int(record.get("pattern_length", 0)), tiebreak


def sorted_records(records: Iterable[Record]) -> List[Record]:
    return sorted(records, key=record_sort_key, reverse=True)


def record_signature(record: Mapping[str, Any]) -> str:
    signature = record.get("pattern_signature")
    if isinstance(signature, str) and signature:
        return signature
    return str(record.get("pattern_kind", "none"))


# --- Record construction ---------------------------------------------------


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
    record: Record = {
        "public_key": public_key_hex,
        "repeater_id": public_key_hex[:REPEATER_ID_HEX_LENGTH],
        "private_key": private_key_hex,
        "seed": seed_hex,
        "score": int(analysis["score"]),
        "score_version": SCORE_VERSION,
        "rarity_bits": float(analysis["rarity_bits"]),
        "pattern_length": int(analysis["pattern_length"]),
        "pattern_start": int(analysis["pattern_start"]),
        "pattern_kind": str(analysis["pattern_kind"]),
        "pattern_family": str(analysis["pattern_family"]),
        "pattern_signature": str(analysis.get("pattern_signature", analysis["pattern_kind"])),
        "reasons": list(analysis["reasons"]),
        "source": source,
        "found_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(found_timestamp)),
        "found_after_attempts": int(attempts_total),
    }
    if matched_prefix:
        record["matched_gpu_prefix"] = matched_prefix
    return record


def make_cpu_record(
    seed: bytes,
    public_key_hex: str,
    analysis: Mapping[str, Any],
    found_timestamp: float,
    attempts_total: int,
) -> Record:
    return make_record_from_material(
        meshcore_private_key_from_seed(seed).hex().upper(),
        seed.hex().upper(),
        public_key_hex,
        analysis,
        found_timestamp,
        attempts_total,
        "generic_cpu",
    )


def validate_record(record: Mapping[str, Any],
                    proven_pairs: Optional[AbstractSet[Tuple[str, str]]] = None) -> Optional[Record]:
    """Re-derive and re-verify every field before trusting a stored record.

    Two verification paths, chosen by what the record carries:

    *Via the seed* (almost every record). If the saved seed re-derives both the
    public key and the expanded private key, the record is proved consistent
    end to end using OpenSSL's Ed25519 and SHA-512.

    *Via ``proven_pairs``* — pairs already re-derived by mc-keygen, which does
    the same scalar multiplication in Rust. The set is keyed on the public and
    private key together, so knowing one good public key cannot wave through a
    record whose private key was altered.

    *Via the independent implementation* (no seed, and no external proof).
    ``keys.py`` contains a from-scratch scalar multiplication for exactly this
    case. It is thorough and about 2,200 times slower, so it must not be the
    default path — running it over a full set of boards costs minutes of
    silent startup.
    """
    public_key_hex = normalize_hex(record.get("public_key"), PUBLIC_KEY_HEX_LENGTH)
    if public_key_hex is None or is_reserved_public_key(public_key_hex):
        return None
    private_key_hex = normalize_hex(record.get("private_key"), 128)
    if private_key_hex is None:
        return None

    seed_hex = normalize_hex(record.get("seed"), 64)
    verified = False
    if seed_hex is not None:
        seed = bytes.fromhex(seed_hex)
        if (public_key_from_seed(seed).hex().upper() == public_key_hex
                and meshcore_private_key_from_seed(seed).hex().upper() == private_key_hex):
            verified = True
        else:
            seed_hex = None  # the seed does not match; do not keep it

    if not verified and proven_pairs and (public_key_hex, private_key_hex) in proven_pairs:
        verified = True

    if not verified:
        try:
            if public_key_from_meshcore_private(
                    bytes.fromhex(private_key_hex)).hex().upper() != public_key_hex:
                return None
        except ValueError:
            return None

    stored_version = record.get("score_version")
    analysis = analyze_public_key(public_key_hex)
    validated = make_record_from_material(
        private_key_hex,
        seed_hex,
        public_key_hex,
        analysis,
        time.time(),
        int(record.get("found_after_attempts") or 0),
        str(record.get("source") or "restored"),
        record.get("matched_gpu_prefix"),
    )
    # Preserve provenance; the score itself is always the current model's.
    for field in ("found_at", "gpu_campaign_elapsed_seconds", "gpu_targets_completed_by_result"):
        if record.get(field) is not None:
            validated[field] = record[field]
    if stored_version != SCORE_VERSION:
        # Older snapshots may omit the field entirely; record that plainly
        # rather than storing None, which reads as "was not rescored".
        validated["rescored_from_version"] = stored_version if stored_version is not None else "unknown"
    return validated


def needs_slow_verification(record: Mapping[str, Any]) -> bool:
    """True when this record has no seed and must be checked by derivation."""
    return normalize_hex(record.get("seed"), 64) is None


def verifiable_pair(record: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """The (public, private) pair to hand to an external verifier, if usable."""
    public_key_hex = normalize_hex(record.get("public_key"), PUBLIC_KEY_HEX_LENGTH)
    private_key_hex = normalize_hex(record.get("private_key"), 128)
    if public_key_hex is None or private_key_hex is None:
        return None
    return public_key_hex, private_key_hex


def hall_rank(board: Mapping[str, Record], record: Mapping[str, Any]) -> int:
    """1-based position this record occupies on a board."""
    key = record_sort_key(record)
    return 1 + sum(1 for other in board.values() if record_sort_key(other) > key)


# --- Board insertion -------------------------------------------------------


def _replace_if_better(board: MutableMapping[str, Record], key: str, record: Record) -> Optional[bool]:
    existing = board.get(key)
    if existing is None:
        return None
    if record_sort_key(record) <= record_sort_key(existing):
        return False
    board[key] = record
    return True


def _evict_worst(board: MutableMapping[str, Record], record: Record) -> bool:
    worst_key, worst = min(board.items(), key=lambda item: record_sort_key(item[1]))
    if record_sort_key(record) <= record_sort_key(worst):
        return False
    del board[worst_key]
    board[str(record["public_key"])] = record
    return True


def insert_by_public_key(
    board: MutableMapping[str, Record],
    record: Record,
    limit: int,
    max_per_signature: int = 0,
) -> bool:
    """Insert keyed on the full public key, honouring the diversity rule."""
    key = str(record["public_key"])
    decided = _replace_if_better(board, key, record)
    if decided is not None:
        return decided

    if max_per_signature > 0:
        signature = record_signature(record)
        group = [
            (candidate_key, candidate)
            for candidate_key, candidate in board.items()
            if record_signature(candidate) == signature
        ]
        if len(group) >= max_per_signature:
            # The group is full: this key must beat the weakest of its own shape.
            worst_key, worst = min(group, key=lambda item: record_sort_key(item[1]))
            if record_sort_key(record) <= record_sort_key(worst):
                return False
            del board[worst_key]
            board[key] = record
            return True

    if len(board) < limit:
        board[key] = record
        return True
    return _evict_worst(board, record)


def insert_unique(board: MutableMapping[str, Record], record: Record, limit: int) -> bool:
    """One entry per repeater ID: the six characters a MeshCore user actually sees."""
    key = str(record["repeater_id"])
    decided = _replace_if_better(board, key, record)
    if decided is not None:
        return decided
    if len(board) < limit:
        board[key] = record
        return True
    worst_key, worst = min(board.items(), key=lambda item: record_sort_key(item[1]))
    if record_sort_key(record) <= record_sort_key(worst):
        return False
    del board[worst_key]
    board[key] = record
    return True


def empty_category_boards(config: BoardConfig) -> CategoryBoards:
    return {family: {} for family in config.families}


def insert_category(categories: CategoryBoards, record: Record, config: BoardConfig) -> Optional[str]:
    family = str(record.get("pattern_family") or pattern_family(str(record.get("pattern_kind", "none"))))
    if family not in config.families:
        return None
    board = categories.setdefault(family, {})
    if insert_by_public_key(board, record, config.top_category_keys):
        return family
    return None


# --- Cutoff ----------------------------------------------------------------


def _board_cutoff(records: Iterable[Record], limit: int, floor: int) -> int:
    values = list(records)
    if len(values) < limit:
        return floor
    return max(floor, min(int(record["score"]) for record in values))


def current_cutoff(
    unique: Mapping[str, Record],
    hall: Mapping[str, Record],
    categories: Optional[Mapping[str, Mapping[str, Record]]],
    config: BoardConfig,
) -> int:
    floor = config.initial_minimum_score
    cutoffs = [
        _board_cutoff(unique.values(), config.top_repeater_ids, floor),
        _board_cutoff(hall.values(), config.top_vanity_keys, floor),
    ]
    if categories:
        cutoffs.extend(
            _board_cutoff(categories.get(family, {}).values(), config.top_category_keys, floor)
            for family in config.families
        )
    return min(cutoffs)


# --- Presentation ----------------------------------------------------------


def ranked(records: Iterable[Record]) -> List[Record]:
    return sorted_records(records)


def public_view(record: Mapping[str, Any], rank: int) -> Dict[str, Any]:
    """A record with every trace of secret material removed."""
    return {
        "rank": rank,
        "repeater_id": record["repeater_id"],
        "public_key": record["public_key"],
        "score": int(record["score"]),
        "rarity_bits": float(record.get("rarity_bits", 0.0)),
        "pattern_length": int(record.get("pattern_length", 0)),
        "pattern_start": int(record.get("pattern_start", 0)),
        "pattern_kind": record.get("pattern_kind", "none"),
        "pattern_family": record.get("pattern_family", "none"),
        "pattern_signature": record.get("pattern_signature", "none"),
        "reasons": list(record.get("reasons", [])),
        "source": record.get("source", "unknown"),
        "found_at": record.get("found_at"),
    }


def signature_histogram(records: Iterable[Record]) -> Counter:
    return Counter(record_signature(record) for record in records)


def board_diversity(records: Sequence[Record]) -> float:
    """Fraction of a board occupied by distinct pattern shapes."""
    if not records:
        return 0.0
    return len(signature_histogram(records)) / len(records)
