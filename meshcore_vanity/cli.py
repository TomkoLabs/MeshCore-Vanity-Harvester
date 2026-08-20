"""Argument parsing, self-tests and the supervisor loop."""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing as mp
import os
import queue
import signal
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence

from . import SCORE_VERSION, project_revision
from .catalog import catalog_summary
from .config import Config, default_data_directory, worker_settings
from .engine_cpu import start_worker
from .engine_gpu import (
    KeygenEngine,
    build_targets,
    describe_backend,
    detect_capabilities,
    find_binary,
    gpu_name,  # noqa: F401 - exposed for callers and tests
    make_pair_verifier,
)
from .keys import self_test as key_self_test
from .leaderboards import (
    Record,
    board_diversity,
    current_cutoff,
    hall_rank,
    insert_by_public_key,
    insert_category,
    insert_unique,
    make_cpu_record,
    make_record_from_material,
    sorted_records,
)
from .report import (
    RestoreProgress,
    Style,
    backend_summary,
    count,
    duration,
    engine_block,
    labelled,
    leaderboard_table,
    match_line,
    ranking_footnote,
    resume_line,
    status_line,
    summary_block,
)
from .scoring import analyze_public_key, quick_candidate, required_rarity_bits
from .storage import (
    acquire_single_instance_lock,
    append_history_event,
    checkpoint,
    restore_state,
)

RARITY_SCALE = 1_000_000


# --- Self tests ------------------------------------------------------------


def _padded(prefix: str) -> str:
    filler = hashlib.sha256((prefix + "|self-test").encode("ascii")).hexdigest().upper()
    return (prefix + filler * 2)[:64]


def run_self_tests(config: Config) -> None:
    key_self_test()

    samples = {
        "random": _padded("C0FFEF"),
        "coffee": _padded("C0FFEE"),
        "coffee2": _padded("C0FFEEC0FFEE"),
        "coffee3": _padded("C0FFEEC0FFEEC0FFEE"),
        "coffee_tail": _padded("C0FFEEEEEEEEEEEE"),
        "514_2": _padded("514514"),
        "514_6": _padded("514514514514514514"),
        "fours": _padded("444444444444"),
        "ones_long": _padded("11111111111111111"),
        "bad_repeat": _padded("BADBADBADBADBAD"),
        "ace_repeat": _padded("ACEACEACEACEACEACE"),
        "sequence": _padded("123456789ABCDEF"),
        "french": _padded("EFFACEE"),
        "run9": _padded("999999999"),
        "run14": _padded("99999999999999"),
        "run37": _padded("9" * 37),
    }
    analyses = {name: analyze_public_key(value) for name, value in samples.items()}
    scores = {name: int(a["score"]) for name, a in analyses.items()}

    checks = [
        (scores["coffee"] > scores["random"], "word scoring"),
        (scores["coffee3"] > scores["coffee2"] > scores["coffee"], "repeated-word length scoring"),
        (scores["coffee_tail"] > scores["coffee"], "word-tail scoring"),
        (scores["514_6"] > scores["514_2"], "514 repetition scoring"),
        (scores["fours"] > scores["coffee"], "long run scoring"),
        (scores["ones_long"] > scores["bad_repeat"], "long single-run priority"),
        (scores["ace_repeat"] > scores["bad_repeat"], "long repeated-word priority"),
        (scores["french"] > scores["coffee"], "French word scoring"),
        (scores["sequence"] > scores["fours"], "sequence scoring"),
        # A longer run of the same character must always win, by a wide margin.
        (scores["run37"] > scores["run14"] > scores["run9"], "run length ordering"),
    ]
    for passed, label in checks:
        if not passed:
            raise RuntimeError(f"{label} self-test failed")

    # The pre-filter must never hide a key that the full scorer would accept.
    for name, value in samples.items():
        score = scores[name]
        if score <= 0:
            continue
        for cutoff in (score, max(0, score - 1), score // 2):
            if not quick_candidate(value, cutoff):
                raise RuntimeError(f"pre-filter rejected qualifying sample '{name}' at cutoff {cutoff}")

    if required_rarity_bits(config.boards.initial_minimum_score) <= 0:
        raise RuntimeError("initial cutoff leaves the pre-filter unable to reject anything")

    config.validate()


# --- Arguments -------------------------------------------------------------


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="meshcore-vanity-harvester",
        description="Continuously find and rank memorable MeshCore identities.",
        epilog="With no options it uses every engine available and resumes where it left off.",
    )
    parser.add_argument("--version", action="store_true",
                        help="print the Git revision of this checkout and exit")
    parser.add_argument("--self-test", action="store_true",
                        help="validate configuration and scoring, then exit")
    parser.add_argument("--top", type=int, nargs="?", const=20, metavar="N",
                        help="show the current leaderboards and exit (default 20 entries)")
    parser.add_argument("--board", default="hall",
                        choices=("hall", "ids", "word", "single_run", "periodic", "all"),
                        help="which board --top shows (default: hall)")
    parser.add_argument("--no-gpu", action="store_true",
                        help="do not use the mc-keygen backend even if it is available")
    parser.add_argument("--cpu-workers", type=int, metavar="N",
                        help="number of generic CPU workers")
    parser.add_argument("--max-runtime", type=float, metavar="SECONDS",
                        help="stop and checkpoint after this long")
    parser.add_argument("--data-dir", type=Path, metavar="PATH",
                        default=Path(os.environ["MESHCORE_VANITY_DATA_DIR"])
                        if os.environ.get("MESHCORE_VANITY_DATA_DIR") else None,
                        help="state directory (or set MESHCORE_VANITY_DATA_DIR)")
    parser.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true",
                        help="disable coloured output")
    return parser.parse_args(argv)


def config_from_arguments(arguments: argparse.Namespace) -> Config:
    if arguments.cpu_workers is not None and arguments.cpu_workers < 1:
        raise ValueError("--cpu-workers must be positive")
    if arguments.max_runtime is not None and arguments.max_runtime <= 0:
        raise ValueError("--max-runtime must be positive")
    if arguments.top is not None and arguments.top < 1:
        raise ValueError("--top must be positive")
    return Config.create(
        data_directory=arguments.data_dir or default_data_directory(),
        cpu_workers=arguments.cpu_workers,
        gpu_enabled=not arguments.no_gpu,
        max_runtime_seconds=arguments.max_runtime or 0.0,
    )


# --- Read-only leaderboard viewer -----------------------------------------


def show_leaderboards(config: Config, style: Style, limit: int, board: str) -> int:
    """Print the saved boards without starting a search."""
    unique, hall, categories, attempts, elapsed, _events, source, _approx, _rescored = restore_state(config)
    if source == "new":
        print(f"No results yet in {config.paths.data_directory}.")
        print("Run ./run.sh to start harvesting.")
        return 0

    print(style.bold("MeshCore vanity identities"))
    print("\n".join(labelled([
        ("Data directory", str(config.paths.data_directory)),
        ("Keys tried", f"{count(attempts)} over {duration(elapsed)}"),
        ("Diversity", f"{board_diversity(sorted_records(hall.values())):.0%} distinct pattern shapes"),
    ], indent="  ")))

    wanted = (("hall", "ids") + config.boards.families) if board == "all" else (board,)
    titles = {
        "hall": "Hall of fame (best overall)",
        "ids": "Best key per repeater ID",
        "word": "Best word patterns",
        "single_run": "Best single-character runs",
        "periodic": "Best repeating patterns",
    }
    for name in wanted:
        records = sorted_records(
            (unique if name == "ids" else hall if name == "hall" else categories.get(name, {})).values()
        )
        print()
        print(style.bold(titles.get(name, name)))
        print("\n".join(leaderboard_table(style, records, limit=limit)))

    print()
    print(ranking_footnote(style))
    print(style.dim(f"  Private keys for any of these: {config.paths.private_key_map}"))
    return 0


def install_early_interrupt_handlers() -> None:
    """Make the program interruptible before the supervisor loop exists.

    Three reasons this is not optional:

    * verifying a large set of saved boards can take a while, and a stack trace
      is a poor answer to Ctrl+C;
    * a service manager sends SIGTERM, which has no default Python handler and
      would kill the process mid-restore without a word;
    * a process started in the background inherits SIGINT as *ignored*, so
      without installing a handler it could not be stopped by signal at all.
    """
    def raise_interrupt(_signum, _frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, raise_interrupt)
        except (AttributeError, OSError, ValueError):
            pass


# --- Main ------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parse_arguments(argv)
    style = Style(enabled=False if arguments.no_colour else None)

    if arguments.version:
        revision = project_revision()
        print(revision if revision else "unversioned (not a Git checkout)")
        return 0

    try:
        config = config_from_arguments(arguments)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    try:
        run_self_tests(config)
    except (RuntimeError, ValueError) as error:
        print(f"Self-test failed: {error}", file=sys.stderr)
        return 1

    if arguments.self_test:
        targets = build_targets(config.gpu)
        binary = find_binary(config.gpu, allow_build=False)
        capabilities = detect_capabilities(binary) if binary else None
        backend = describe_backend(config.gpu, binary, capabilities)
        print(style.good("Self-test passed."))
        print("\n".join(labelled([
            ("Dictionary", catalog_summary()),
            ("Exact prefixes", f"{len(targets):,} targets"),
            ("Scoring model", f"rev {SCORE_VERSION}, calibrated"),
            ("Backend", backend.headline),
        ], indent="  ")))
        if backend.remedy:
            print(style.dim(f"  → {backend.remedy}"))
        return 0

    if arguments.top is not None:
        return show_leaderboards(config, style, arguments.top, arguments.board)

    try:
        lock_file = acquire_single_instance_lock(config.paths)
    except RuntimeError as error:
        print(f"{error}", file=sys.stderr)
        return 3

    # Everything up to the first checkpoint is read-only, so an interrupt here
    # can simply leave. Reporting that plainly beats a stack trace.
    install_early_interrupt_handlers()
    try:
        # Locate the backend before restoring: it can re-derive saved keys far
        # faster than Python can, and restoring is where that matters most.
        binary = find_binary(config.gpu)

        (unique, hall, categories, attempts_before, elapsed_before,
         leaderboard_events, recovery_source, statistics_approximate, rescored) = restore_state(
            config, RestoreProgress(style), make_pair_verifier(binary))
        cutoff_value = current_cutoff(unique, hall, categories, config.boards)

        context = mp.get_context("spawn")
        stop_event = context.Event()
        result_queue = context.Queue(maxsize=config.result_queue_size)
        counter = context.Value("Q", 0)
        cutoff = context.Value("Q", cutoff_value)

        engine_queue: "queue.Queue[Dict[str, object]]" = queue.Queue()
        engine: Optional[KeygenEngine] = None
        if binary is not None:
            engine = KeygenEngine(binary, config, engine_queue)
            engine.set_cutoff(cutoff_value)
            seen = {record["public_key"] for record in hall.values()}
            seen |= {record["public_key"] for board in categories.values()
                     for record in board.values()}
            for public_key in seen:
                engine.register_public_key(str(public_key))
    except KeyboardInterrupt:
        print("\nStopped before harvesting began. Nothing was changed.", file=sys.stderr)
        try:
            lock_file.close()
        except OSError:
            pass
        return 130

    backend = engine.status if engine is not None else describe_backend(config.gpu, binary, None)
    target_count = len(engine.targets) if engine else 0

    # Only hold a core back for feeding the GPU if a GPU is actually being fed.
    # Deciding this before the backend was known left cores idle on CPU-only
    # machines, which are exactly the ones that can least afford it.
    config = replace(config, cpu=config.cpu.resolved(backend_active=backend.active))

    # --- startup report ---------------------------------------------------
    revision = project_revision()
    print(style.bold("MeshCore Vanity Harvester")
          + (style.dim(f"   {revision}") if revision else ""))
    print()
    print("\n".join(engine_block(style, config.cpu.workers, len(config.cpu.available_cpu_ids),
                                 backend, target_count)))
    print()
    print("\n".join(resume_line(style, recovery_source, len(unique), len(hall),
                                attempts_before, elapsed_before, rescored)))
    print()
    print("\n".join(labelled([
        ("Saving to", str(config.paths.data_directory)),
        ("Boards", f"{config.boards.top_repeater_ids} repeater IDs, "
                   f"{config.boards.top_vanity_keys} hall (max {config.boards.max_per_signature} per shape), "
                   f"{config.boards.top_category_keys} per category"),
    ], indent="  ")))
    print()
    print(style.dim("Searching. Press Ctrl+C to stop; everything is saved on exit."))
    print(flush=True)

    shutdown_requested = False

    def request_shutdown(_signum=None, _frame=None) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        stop_event.set()
        if engine is not None:
            engine.stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    try:
        signal.signal(signal.SIGTERM, request_shutdown)
    except (AttributeError, OSError):
        pass

    settings = worker_settings(config)
    workers = [
        start_worker(context, index, settings, stop_event, result_queue, counter, cutoff)
        for index in range(config.cpu.workers)
    ]
    restart_times: Deque[float] = deque()
    if engine is not None:
        engine.start()

    start_monotonic = time.monotonic()
    last_status = start_monotonic
    last_checkpoint = start_monotonic
    last_urgent = 0.0
    last_health = start_monotonic
    last_announced = 0.0
    cpu_found = 0
    last_backup = 0.0
    checkpoint_failures = 0
    changed = bool(unique or hall)

    def cpu_attempts() -> int:
        with counter.get_lock():
            return int(counter.value)

    def save(attempts_total: int, elapsed_total: float) -> bool:
        """Checkpoint, tolerating a full or unwritable disk.

        Losing a checkpoint is recoverable — the boards are in memory and the
        next attempt may succeed. Dying because of one is not.
        """
        nonlocal last_backup, checkpoint_failures
        now_wall = time.monotonic()
        keep_backup = now_wall - last_backup >= config.backup_interval_seconds
        try:
            checkpoint(unique, hall, categories, attempts_total, elapsed_total,
                       leaderboard_events, recovery_source, statistics_approximate,
                       config, keep_backup=keep_backup)
        except OSError as error:
            checkpoint_failures += 1
            if checkpoint_failures in (1, 10) or checkpoint_failures % 100 == 0:
                print(style.warn(f"  Could not save state ({error}). "
                                 f"Still searching; will retry. [{checkpoint_failures}]"),
                      file=sys.stderr, flush=True)
            return False
        if checkpoint_failures:
            print(style.good("  State saved again."), file=sys.stderr, flush=True)
            checkpoint_failures = 0
        if keep_backup:
            last_backup = now_wall
        return True

    def announce(record: Record, keys_per_second=None, always: bool = False,
                 family: Optional[str] = None) -> None:
        """Print a find worth interrupting the status line for.

        Rank against the hall of fame *and* against the record's own category
        board. Once the hall fills with deep GPU prefixes a strong CPU find can
        no longer reach the top ten overall, and announcing only on the hall
        made the CPU workers look idle for hours while they were still working.
        """
        nonlocal last_announced
        rank = hall_rank(hall, record)
        notable = rank <= config.notable_rank
        if not notable and family:
            board = categories.get(family) or {}
            notable = hall_rank(board, record) <= config.notable_rank
        if not (always or notable):
            return
        now = time.monotonic()
        if (not always and rank > 1
                and now - last_announced < config.notable_min_interval_seconds):
            return
        last_announced = now
        print(match_line(style, record, keys_per_second, rank),
              file=sys.stderr, flush=True)

    def accept(record: Record, now: float) -> Optional[str]:
        """Returns the category family that took it, "" if only other boards
        did, or None if the record was rejected outright."""
        nonlocal leaderboard_events, changed, last_checkpoint, last_urgent
        # A key with no measurable pattern is not a vanity key, whichever engine
        # produced it. Rarity of zero means the pattern is no more surprising
        # than randomness once its position and reference class are charged for;
        # aesthetic bonus alone must not buy a place on a rarity-first board.
        # CPU workers already filter on the cutoff, but backend results bypass
        # that, so guard here rather than trusting the target list.
        if float(record.get("rarity_bits", 0.0)) <= 0.0 or int(record.get("score", 0)) <= 0:
            return None
        boards: List[str] = []
        if insert_unique(unique, record, config.boards.top_repeater_ids):
            boards.append("repeater_ids")
        if insert_by_public_key(hall, record, config.boards.top_vanity_keys,
                                config.boards.max_per_signature):
            boards.append("vanity_hall_of_fame")
        family = insert_category(categories, record, config.boards)
        if family is not None:
            boards.append(f"{family}_board")
        if not boards:
            return None

        leaderboard_events += 1
        changed = True
        attempts_total = attempts_before + cpu_attempts()
        append_history_event(record, boards, attempts_total, config.paths)
        new_cutoff = current_cutoff(unique, hall, categories, config.boards)
        cutoff.value = new_cutoff
        if engine is not None:
            engine.set_cutoff(new_cutoff)
        if int(record["score"]) >= config.urgent_checkpoint_score and \
                now - last_urgent >= config.urgent_checkpoint_min_interval_seconds:
            if save(attempts_total, elapsed_before + (now - start_monotonic)):
                changed = False
                last_checkpoint = now
            last_urgent = now
        return family or ""

    exit_code = 0
    try:
        while not shutdown_requested:
            now = time.monotonic()
            if config.max_runtime_seconds and now - start_monotonic >= config.max_runtime_seconds:
                request_shutdown()
                break

            try:
                seed, public_key_hex, analysis, found_timestamp = result_queue.get(timeout=0.15)
                record = make_cpu_record(seed, public_key_hex, analysis,
                                         float(found_timestamp), attempts_before + cpu_attempts())
                taken = accept(record, time.monotonic())
                if taken is not None:
                    cpu_found += 1
                    announce(record, family=taken or None)
                if engine is not None:
                    engine.register_public_key(public_key_hex)
            except queue.Empty:
                pass

            for _ in range(32):
                try:
                    message = engine_queue.get_nowait()
                except queue.Empty:
                    break
                kind = message.get("type")
                if kind == "keygen_result":
                    record = make_record_from_material(
                        str(message["private_key_hex"]), message.get("seed_hex"),
                        str(message["public_key_hex"]), message["analysis"],
                        float(message["found_timestamp"]), attempts_before + cpu_attempts(),
                        f"mc_keygen_{engine.mode}" if engine else "mc_keygen",
                        str(message["matched_prefix"]),
                    )
                    record["gpu_campaign_elapsed_seconds"] = round(
                        float(message.get("gpu_elapsed_seconds", 0.0)), 3)
                    record["gpu_targets_completed_by_result"] = list(
                        message.get("newly_completed_targets", []))
                    if accept(record, time.monotonic()) is not None:
                        announce(record, message.get("gpu_measured_keys_per_second"), always=True)
                else:
                    print(style.dim(f"  mc-keygen: {message.get('message', kind)}"),
                          file=sys.stderr, flush=True)

            now = time.monotonic()
            elapsed_this_run = now - start_monotonic

            if now - last_status >= config.status_interval_seconds:
                attempts_run = cpu_attempts()
                ordered = sorted_records(hall.values())
                if engine is None:
                    summary = backend_summary(False, "off", 0, 0, 0, 0.0, (), 0.0)
                else:
                    snapshot = engine.snapshot()
                    active_campaign = snapshot.get("active_campaign") or ()
                    campaign_elapsed = now - float(snapshot.get("active_started") or now)
                    summary = backend_summary(
                        True, engine.mode, len(snapshot["found_prefixes"]), len(engine.targets),
                        snapshot["matches_total"], snapshot["keys_per_second"],
                        active_campaign, campaign_elapsed,
                    )
                print(status_line(
                    style, elapsed_before + elapsed_this_run,
                    attempts_before + attempts_run,
                    attempts_run / elapsed_this_run if elapsed_this_run else 0.0,
                    len(unique), config.boards.top_repeater_ids,
                    len(hall), config.boards.top_vanity_keys,
                    int(cutoff.value) / RARITY_SCALE,
                    ordered[0] if ordered else None,
                    summary,
                    cpu_found,
                ), file=sys.stderr, flush=True)
                last_status = now

            if changed and now - last_checkpoint >= config.checkpoint_interval_seconds:
                if save(attempts_before + cpu_attempts(), elapsed_before + elapsed_this_run):
                    changed = False
                last_checkpoint = now

            if now - last_health >= config.worker_health_interval_seconds:
                for index, process in enumerate(workers):
                    if process.is_alive() or shutdown_requested:
                        continue
                    restart_now = time.monotonic()
                    restart_times.append(restart_now)
                    while restart_times and restart_now - restart_times[0] > config.worker_restart_window_seconds:
                        restart_times.popleft()
                    if len(restart_times) > config.max_worker_restarts:
                        raise RuntimeError("CPU workers repeatedly exited")
                    workers[index] = start_worker(context, index, settings, stop_event,
                                                  result_queue, counter, cutoff)
                    print(style.warn(f"  Restarted CPU worker {index + 1}."),
                          file=sys.stderr, flush=True)
                last_health = now

    except KeyboardInterrupt:
        request_shutdown()
        exit_code = 130
    except Exception as error:  # noqa: BLE001 - always checkpoint before dying
        request_shutdown()
        print(f"Fatal error: {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        stop_event.set()
        if engine is not None:
            engine.stop()
        for process in workers:
            process.join(timeout=5.0)
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)

        attempts_run = cpu_attempts()
        elapsed_run = time.monotonic() - start_monotonic
        attempts_total = attempts_before + attempts_run
        elapsed_total = elapsed_before + elapsed_run
        # The final save always takes a backup: it is the copy a crashed run
        # would be recovered from.
        last_backup = 0.0
        save(attempts_total, elapsed_total)
        try:
            lock_file.close()
        except OSError:
            pass

    backend_stats = None
    if engine is not None:
        snapshot = engine.snapshot()
        backend_stats = {
            "matches": snapshot["matches_total"],
            "found": len(snapshot["found_prefixes"]),
            "total": len(engine.targets),
            "keys_per_second": snapshot["keys_per_second"],
        }
    ordered = sorted_records(hall.values())
    print("\n".join(summary_block(
        style, attempts_run, attempts_total, elapsed_run, elapsed_total,
        backend_stats, ordered, board_diversity(ordered), config.paths,
    )))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
