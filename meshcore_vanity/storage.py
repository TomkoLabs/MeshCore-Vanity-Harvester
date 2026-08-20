"""Atomic, integrity-hashed persistence and recovery.

Private key material is the whole value of this program's output, so writes are
crash-safe rather than merely convenient: a copy of the previous file is taken
first, the new content is written to a temporary file with the final mode
already applied, fsynced, then renamed into place, and the directory itself is
fsynced so the rename survives power loss.

Three layers of recovery exist, tried in order: the live snapshot, its backup,
and the append-only history journal that records every leaderboard event.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import SCORE_VERSION, STATE_FORMAT_VERSION, project_revision
from .config import BoardConfig, Config, Paths, PUBLIC_KEY_HEX_LENGTH
from .keys import normalize_hex
from .leaderboards import (
    Board,
    CategoryBoards,
    Record,
    empty_category_boards,
    insert_by_public_key,
    insert_unique,
    insert_category,
    needs_slow_verification,
    public_view,
    verifiable_pair,
    ranked,
    validate_record,
)

PUBLIC_MODE = 0o644
PRIVATE_MODE = 0o600
DIRECTORY_MODE = 0o700

RestoredState = Tuple[Board, CategoryBoards, int, float, int]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --- Integrity -------------------------------------------------------------


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
    actual = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return hmac.compare_digest(actual, expected)


# --- Atomic writes ---------------------------------------------------------


def ensure_secure_directory(paths: Paths) -> None:
    paths.data_directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    os.chmod(paths.data_directory, DIRECTORY_MODE)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: str, private: bool, keep_backup: bool = True) -> None:
    """Back up, write, fsync and rename. One implementation for every file.

    ``keep_backup`` is what makes long runs cheap. The rename itself is atomic,
    the content carries an integrity hash, and the history journal can rebuild
    the boards, so copying the previous file aside on *every* checkpoint doubles
    disk traffic to insure against a risk already covered three ways. Callers
    take a backup periodically instead.
    """
    mode = PRIVATE_MODE if private else PUBLIC_MODE
    backup_path = Paths.backup(path)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    backup_temporary = backup_path.with_name(f".{backup_path.name}.tmp.{os.getpid()}")

    if keep_backup and path.exists():
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
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: Mapping[str, Any], private: bool,
                      keep_backup: bool = True) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=False) + "\n", private, keep_backup)


def read_verified_json(path: Path) -> Optional[Dict[str, Any]]:
    for candidate in (path, Paths.backup(path)):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and verify_integrity_hash(payload):
            return payload
    return None


# --- Single-instance lock --------------------------------------------------


def acquire_single_instance_lock(paths: Paths):
    ensure_secure_directory(paths)
    lock_file = paths.lock.open("a+", encoding="utf-8")
    os.chmod(paths.lock, PRIVATE_MODE)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise RuntimeError(
            f"another harvester is already using {paths.data_directory}"
        ) from error
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()} started={utc_now()}\n")
    lock_file.flush()
    os.fsync(lock_file.fileno())
    return lock_file


# --- Checkpoint ------------------------------------------------------------


def build_state_payload(
    unique: Mapping[str, Record],
    hall: Mapping[str, Record],
    categories: Mapping[str, Mapping[str, Record]],
    attempts_total: int,
    elapsed_total: float,
    events: int,
    recovery_source: str,
    statistics_approximate: bool,
    config: Config,
    private: bool,
) -> Dict[str, Any]:
    board_config = config.boards
    meta = {
        "state_format_version": STATE_FORMAT_VERSION,
        "source_revision": project_revision(),
        "score_version": SCORE_VERSION,
        "generated_at": utc_now(),
        "cpu_attempts_total": int(attempts_total),
        "elapsed_seconds_total": round(float(elapsed_total), 3),
        "leaderboard_events": int(events),
        "recovery_source": recovery_source,
        "statistics_approximate": bool(statistics_approximate),
        "includes_secret_material": bool(private),
        "board_limits": {
            "repeater_ids": board_config.top_repeater_ids,
            "vanity_hall_of_fame": board_config.top_vanity_keys,
            "category": board_config.top_category_keys,
            "max_per_signature": board_config.max_per_signature,
        },
    }

    def render(records: Iterable[Record]) -> list:
        ordered = ranked(records)
        if private:
            return [dict(record, rank=index + 1) for index, record in enumerate(ordered)]
        return [public_view(record, index + 1) for index, record in enumerate(ordered)]

    payload: Dict[str, Any] = dict(meta)
    payload["repeater_ids"] = render(unique.values())
    payload["vanity_hall_of_fame"] = render(hall.values())
    payload["categories"] = {
        family: render(categories.get(family, {}).values()) for family in board_config.families
    }
    return add_integrity_hash(payload)


def write_lookup_outputs(unique: Mapping[str, Record], paths: Paths,
                         keep_backup: bool = True) -> None:
    """A scannable public index, and a separate private-key lookup table."""
    ordered = ranked(unique.values())
    lines = []
    for index, record in enumerate(ordered):
        lines.append(json.dumps({
            "rank": index + 1,
            "repeater_id": record["repeater_id"],
            "public_key": record["public_key"],
            "score": int(record["score"]),
            "rarity_bits": float(record.get("rarity_bits", 0.0)),
            "pattern_kind": record.get("pattern_kind", "none"),
            "pattern_signature": record.get("pattern_signature", "none"),
            "reason": (record.get("reasons") or [""])[0],
        }, sort_keys=False))
    atomic_write(paths.public_id_list, "\n".join(lines) + ("\n" if lines else ""),
                 private=False, keep_backup=keep_backup)

    mapping = {
        str(record["public_key"]): {
            "repeater_id": record["repeater_id"],
            "private_key": record["private_key"],
            "seed": record.get("seed"),
            "score": int(record["score"]),
        }
        for record in ordered
    }
    atomic_write_json(
        paths.private_key_map,
        add_integrity_hash({
            "state_format_version": STATE_FORMAT_VERSION,
            "generated_at": utc_now(),
            "warning": "Anyone with these private keys controls the corresponding MeshCore identities.",
            "count": len(mapping),
            "keys": mapping,
        }),
        private=True,
        keep_backup=keep_backup,
    )


def checkpoint(
    unique: Mapping[str, Record],
    hall: Mapping[str, Record],
    categories: Mapping[str, Mapping[str, Record]],
    attempts_total: int,
    elapsed_total: float,
    events: int,
    recovery_source: str,
    statistics_approximate: bool,
    config: Config,
    keep_backup: bool = True,
) -> None:
    ensure_secure_directory(config.paths)
    common = (unique, hall, categories, attempts_total, elapsed_total, events,
              recovery_source, statistics_approximate, config)
    atomic_write_json(config.paths.private_state, build_state_payload(*common, private=True),
                      private=True, keep_backup=keep_backup)
    atomic_write_json(config.paths.public_state, build_state_payload(*common, private=False),
                      private=False, keep_backup=keep_backup)
    write_lookup_outputs(unique, config.paths, keep_backup=keep_backup)


MAX_HISTORY_BYTES = 32 * 1024 * 1024


def rotate_history_if_large(paths: Paths, limit: int = MAX_HISTORY_BYTES) -> bool:
    """Bound the append-only journal so a long run cannot fill the disk.

    The journal exists to rebuild the boards when a snapshot is unreadable, and
    only the most recent entries can matter for that — the boards hold a bounded
    number of keys. Left alone it grows forever at roughly 1.5 KiB per find.
    One previous generation is kept, so recovery still reaches back well beyond
    the current boards.
    """
    try:
        if paths.history.stat().st_size < limit:
            return False
    except OSError:
        return False
    previous = paths.history.with_name(paths.history.name + ".1")
    try:
        os.replace(paths.history, previous)
        os.chmod(previous, PRIVATE_MODE)
    except OSError:
        return False
    return True


def history_paths(paths: Paths) -> Tuple[Path, ...]:
    """Journal generations, oldest first, for recovery."""
    previous = paths.history.with_name(paths.history.name + ".1")
    return tuple(p for p in (previous, paths.history) if p.is_file())


def append_history_event(
    record: Mapping[str, Any],
    boards: Sequence[str],
    attempts_total: int,
    paths: Paths,
) -> None:
    ensure_secure_directory(paths)
    rotate_history_if_large(paths)
    event = {
        "recorded_at": utc_now(),
        "boards": list(boards),
        "cpu_attempts_total": int(attempts_total),
        "score_version": SCORE_VERSION,
        "record": dict(record),
    }
    descriptor = os.open(str(paths.history), os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_MODE)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
            output.write(json.dumps(event, sort_keys=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
    finally:
        try:
            os.chmod(paths.history, PRIVATE_MODE)
        except OSError:
            pass


# --- Restore ---------------------------------------------------------------


def _insert_restored(
    record: Record,
    unique: Board,
    hall: Board,
    categories: CategoryBoards,
    config: BoardConfig,
) -> None:
    insert_unique(unique, record, config.top_repeater_ids)
    insert_by_public_key(hall, record, config.top_vanity_keys, config.max_per_signature)
    insert_category(categories, record, config)


def _restore_records(records, config: BoardConfig, progress=None, verifier=None):
    """Validate once per distinct key, then fill every board.

    One key normally appears on several boards, and verification is the most
    expensive part of startup, so results are memoised by public key. Records
    that can only be checked the slow way are counted first and reported, so a
    genuinely slow restore looks like work rather than a hang.
    """
    unique: Board = {}
    hall: Board = {}
    categories = empty_category_boards(config)
    rescored_keys: Set[str] = set()
    validated_by_key: Dict[str, Optional[Record]] = {}

    entries = [raw for raw in records if isinstance(raw, Mapping)]
    distinct: Dict[str, Mapping[str, Any]] = {}
    for raw in entries:
        key = normalize_hex(raw.get("public_key"), PUBLIC_KEY_HEX_LENGTH)
        if key is not None and key not in distinct:
            distinct[key] = raw

    seedless = [raw for raw in distinct.values() if needs_slow_verification(raw)]

    # mc-keygen can re-derive a whole batch of keys in the time Python takes for
    # one. When it is available the expensive path all but disappears; when it
    # is not, or it fails, every record still gets checked here.
    proven: Set[Tuple[str, str]] = set()
    if verifier is not None and seedless:
        pairs = [pair for pair in (verifiable_pair(raw) for raw in seedless) if pair]
        if pairs:
            try:
                proven = set(verifier(pairs))
            except Exception:  # noqa: BLE001 - verification is an optimisation
                proven = set()

    slow = sum(1 for raw in seedless
               if (verifiable_pair(raw) or ("", "")) not in proven)
    if progress is not None:
        progress("start", len(distinct), slow)

    checked = 0
    for key, raw in distinct.items():
        validated_by_key[key] = validate_record(raw, proven)
        checked += 1
        if progress is not None:
            progress("step", checked, len(distinct))

    for raw in entries:
        key = normalize_hex(raw.get("public_key"), PUBLIC_KEY_HEX_LENGTH)
        validated = validated_by_key.get(key) if key else None
        if validated is None:
            continue
        if validated.get("rescored_from_version") is not None:
            rescored_keys.add(str(validated["public_key"]))
        _insert_restored(dict(validated), unique, hall, categories, config)

    if progress is not None:
        progress("done", len(distinct), slow)
    return unique, hall, categories, len(rescored_keys)


# Section names have changed across releases (`repeater_id_top50` in v7,
# `repeater_ids` since v8), and an earlier reader that looked for fixed names
# silently ignored older snapshots. Rather than maintain a list of aliases that
# will go stale again, records are collected structurally: any list of objects
# carrying a public key is a board, wherever it sits in the file.
def _collect_records(payload: Any, depth: int = 0) -> List[Mapping[str, Any]]:
    records: List[Mapping[str, Any]] = []
    if depth > 3:
        return records
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, Mapping) and "public_key" in item:
                records.append(item)
            elif isinstance(item, (list, Mapping)):
                records.extend(_collect_records(item, depth + 1))
    elif isinstance(payload, Mapping):
        for value in payload.values():
            if isinstance(value, (list, Mapping)):
                records.extend(_collect_records(value, depth + 1))
    return records


def _first_number(payload: Mapping[str, Any], names: Sequence[str], cast, default):
    for name in names:
        value = payload.get(name)
        if value is not None:
            try:
                return cast(value)
            except (TypeError, ValueError):
                continue
    return default


def load_state_file(path: Path, config: BoardConfig, progress=None, verifier=None):
    payload = read_verified_json(path)
    if payload is None:
        return None
    records = _collect_records(payload)
    unique, hall, categories, rescored = _restore_records(records, config, progress, verifier)
    if not unique and not hall:
        return None
    return (
        unique, hall, categories,
        _first_number(payload, ("cpu_attempts_total", "attempts_total"), int, 0),
        _first_number(payload, ("elapsed_seconds_total",), float, 0.0),
        _first_number(payload, ("leaderboard_events",), int, 0),
        rescored,
    )


def recover_from_history(paths: Paths, config: BoardConfig, progress=None, verifier=None):
    generations = history_paths(paths)
    if not generations:
        return None
    records = []
    attempts = 0
    events = 0
    for path in generations:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    record = event.get("record")
                    if isinstance(record, dict):
                        records.append(record)
                        events += 1
                    attempts = max(attempts, int(event.get("cpu_attempts_total") or 0))
        except OSError:
            continue
    if not records:
        return None
    unique, hall, categories, rescored = _restore_records(records, config, progress, verifier)
    if not unique and not hall:
        return None
    return unique, hall, categories, attempts, 0.0, events, rescored


def restore_state(config: Config, progress=None, verifier=None):
    """Return boards plus provenance: (unique, hall, categories, attempts,
    elapsed, events, source, statistics_approximate, rescored_count)."""
    board_config = config.boards
    # Only the private snapshot can be restored from: the public one carries no
    # key material, so a record from it could never be validated or used.
    loaded = load_state_file(config.paths.private_state, board_config, progress, verifier)
    if loaded is not None:
        unique, hall, categories, attempts, elapsed, events, rescored = loaded
        return unique, hall, categories, attempts, elapsed, events, "private_state", False, rescored

    recovered = recover_from_history(config.paths, board_config, progress, verifier)
    if recovered is not None:
        unique, hall, categories, attempts, elapsed, events, rescored = recovered
        return unique, hall, categories, attempts, elapsed, events, "history_journal", True, rescored

    return {}, {}, empty_category_boards(board_config), 0, 0.0, 0, "new", False, 0
