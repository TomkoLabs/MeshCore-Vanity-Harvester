"""Generic CPU discovery workers.

Each worker draws random Ed25519 seeds, derives the public key, and runs the
cheap screening filter. Only keys that survive screening pay for a full
analysis. Settings travel to the worker explicitly rather than through module
globals, because a spawned child re-imports the package with defaults and would
otherwise silently disagree with its parent about the configuration in force.
"""

from __future__ import annotations

import os
import queue
import signal
import time
from typing import Any, Mapping

from .keys import public_key_from_seed
from .scoring import analyze_public_key, quick_candidate

RESERVED_FIRST_BYTES = (0x00, 0xFF)


def add_to_shared_counter(counter, amount: int) -> None:
    with counter.get_lock():
        counter.value += amount


def worker_main(
    worker_index: int,
    settings: Mapping[str, Any],
    stop_event,
    result_queue,
    counter,
    cutoff,
) -> None:
    # The supervisor owns Ctrl+C; workers stop when the shared event is set.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        result_queue.cancel_join_thread()
    except (AttributeError, OSError):
        pass

    nice_increment = int(settings.get("nice_increment", 0))
    if nice_increment:
        try:
            os.nice(nice_increment)
        except OSError:
            pass

    worker_cpu_ids = list(settings.get("worker_cpu_ids") or [])
    if settings.get("pin_workers") and worker_cpu_ids:
        try:
            os.sched_setaffinity(0, {worker_cpu_ids[worker_index % len(worker_cpu_ids)]})
        except OSError:
            pass

    seed_batch = int(settings.get("random_seed_batch", 512))
    counter_batch = int(settings.get("counter_batch_size", 10_000))
    refresh_interval = int(settings.get("cutoff_refresh_interval", 1_024))

    local_count = 0
    local_cutoff = int(cutoff.value)
    refresh_countdown = refresh_interval

    try:
        while not stop_event.is_set():
            random_block = os.urandom(32 * seed_batch)
            for offset in range(0, len(random_block), 32):
                if stop_event.is_set():
                    break
                seed = random_block[offset:offset + 32]
                public_key = public_key_from_seed(seed)
                local_count += 1

                if public_key[0] not in RESERVED_FIRST_BYTES:
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
                    refresh_countdown = refresh_interval
                if local_count >= counter_batch:
                    add_to_shared_counter(counter, local_count)
                    local_count = 0
    except KeyboardInterrupt:
        pass
    finally:
        if local_count:
            add_to_shared_counter(counter, local_count)


def start_worker(context, index: int, settings: Mapping[str, Any], stop_event, result_queue, counter, cutoff):
    process = context.Process(
        target=worker_main,
        args=(index, dict(settings), stop_event, result_queue, counter, cutoff),
        daemon=True,
    )
    process.start()
    return process


def measure_keys_per_second(sample: int = 4_000) -> float:
    """Throughput of one core, used for the startup report."""
    block = os.urandom(32 * sample)
    started = time.monotonic()
    for offset in range(0, len(block), 32):
        public_key_from_seed(block[offset:offset + 32])
    elapsed = time.monotonic() - started
    return sample / elapsed if elapsed > 0 else 0.0
