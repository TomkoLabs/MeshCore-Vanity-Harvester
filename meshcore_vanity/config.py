"""Runtime configuration and file paths.

Everything that used to be a mutable module-level global lives here as a frozen
dataclass. Configuration is built once in the parent process and passed
explicitly to every consumer, including spawned worker processes, so a child
can never silently disagree with its parent about the settings in force.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, Sequence, Tuple

PUBLIC_KEY_HEX_LENGTH = 64
REPEATER_ID_HEX_LENGTH = 6

# Rarity is measured in bits and scaled so that one bit dominates every
# aesthetic tie-breaker combined. See scoring.py for what a "bit" means here.
RARITY_SCORE_PER_BIT = 1_000_000
MAX_PRIMARY_SUBJECTIVE_BONUS = 700_000
MAX_SECONDARY_BONUS = 250_000
MAX_TOTAL_TIEBREAKER_BONUS = MAX_PRIMARY_SUBJECTIVE_BONUS + MAX_SECONDARY_BONUS

DATA_DIRECTORY_NAME = "data"
PUBLIC_STATE_FILENAME = "leaderboards_public.json"
PUBLIC_TEXT_FILENAME = "leaderboards_public.txt"
PRIVATE_STATE_FILENAME = "leaderboards_private.json"
HISTORY_FILENAME = "leaderboard_history.private.jsonl"
PUBLIC_ID_LIST_FILENAME = "repeater_ids_public.jsonl"
PRIVATE_KEY_MAP_FILENAME = "private_keys_by_public_key.json"
GPU_PROGRESS_FILENAME = "gpu_campaign_state.json"
LOCK_FILENAME = ".harvester.lock"

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = PACKAGE_DIRECTORY.parent

CATEGORY_BOARD_FAMILIES: Tuple[str, ...] = ("word", "single_run", "periodic", "sequence")
NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def default_node_id() -> str:
    """A stable, human-readable identity for this compute source."""
    hostname = socket.gethostname().strip()
    return hostname if NODE_ID_PATTERN.fullmatch(hostname) else "local"


def is_source_checkout() -> bool:
    """True when the package is being run from a cloned repository.

    An editable install still resolves to the checkout, so the marker has to be
    a file that only ever exists there. pyproject.toml is present in every
    checkout and absent from site-packages.
    """
    return (PROJECT_DIRECTORY / "pyproject.toml").is_file()


def default_data_directory() -> Path:
    """A checkout keeps state beside the code; a system install uses XDG state."""
    if is_source_checkout():
        return PROJECT_DIRECTORY / DATA_DIRECTORY_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "meshcore-vanity-harvester"


@dataclass(frozen=True)
class Paths:
    """Every file the harvester reads or writes, derived from one directory."""

    data_directory: Path

    @classmethod
    def for_directory(cls, directory: Path) -> "Paths":
        return cls(data_directory=directory.expanduser().resolve())

    def _child(self, name: str) -> Path:
        return self.data_directory / name

    @property
    def public_state(self) -> Path:
        return self._child(PUBLIC_STATE_FILENAME)

    @property
    def public_text(self) -> Path:
        return self._child(PUBLIC_TEXT_FILENAME)

    @property
    def private_state(self) -> Path:
        return self._child(PRIVATE_STATE_FILENAME)

    @property
    def history(self) -> Path:
        return self._child(HISTORY_FILENAME)

    @property
    def public_id_list(self) -> Path:
        return self._child(PUBLIC_ID_LIST_FILENAME)

    @property
    def private_key_map(self) -> Path:
        return self._child(PRIVATE_KEY_MAP_FILENAME)

    @property
    def gpu_progress(self) -> Path:
        return self._child(GPU_PROGRESS_FILENAME)

    @property
    def lock(self) -> Path:
        return self._child(LOCK_FILENAME)

    @staticmethod
    def backup(path: Path) -> Path:
        return path.with_name(f"{path.name}.bak")


def _available_cpu_ids() -> Tuple[int, ...]:
    try:
        return tuple(sorted(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return tuple(range(os.cpu_count() or 2))


@dataclass(frozen=True)
class CpuConfig:
    """Generic CPU discovery settings."""

    available_cpu_ids: Tuple[int, ...] = field(default_factory=_available_cpu_ids)
    workers: int = 0          # 0 means "derive from available_cpu_ids"
    reserved_cpus: int = 0    # 0 means "derive from whether a backend is running"
    requested_workers: int = 0  # what the user asked for, 0 for automatic
    random_seed_batch: int = 512
    pin_workers: bool = True
    nice_increment: int = 5
    counter_batch_size: int = 10_000
    cutoff_refresh_interval: int = 1_024

    # Cores held back from the search. Feeding a GPU needs a core of its own;
    # everything else only needs enough headroom to keep the machine usable,
    # and the workers run at a raised nice value precisely so they can be
    # preempted. Reserving a quarter of a large machine, as an earlier version
    # did, simply left cores idle.
    reserved_with_backend: int = 2
    reserved_without_backend: int = 1

    def resolved(self, backend_active: bool = True) -> "CpuConfig":
        cpus = self.available_cpu_ids or (0,)
        requested = self.requested_workers or self.workers
        if self.reserved_cpus:
            reserved = self.reserved_cpus
        else:
            reserved = self.reserved_with_backend if backend_active else self.reserved_without_backend
        reserved = min(reserved, max(0, len(cpus) - 1))
        workers = requested or max(1, len(cpus) - reserved)
        workers = max(1, min(workers, len(cpus)))
        return replace(self, workers=workers, reserved_cpus=reserved,
                       requested_workers=requested)

    @property
    def worker_cpu_ids(self) -> Tuple[int, ...]:
        cpus = self.available_cpu_ids or (0,)
        return cpus[: max(1, self.workers or 1)]

    @property
    def gpu_host_cpu_ids(self) -> Tuple[int, ...]:
        cpus = self.available_cpu_ids or (0,)
        return cpus[len(self.worker_cpu_ids):] or cpus[-1:]


@dataclass(frozen=True)
class GpuConfig:
    """Native prefix harvesting and optional legacy campaign settings."""

    enabled: bool = True
    broad_harvest: bool = True
    harvest_minimum_bits: float = 28.0
    run_startup_self_test: bool = True
    self_test_timeout_seconds: float = 120.0
    device_index: str = "0"
    cuda_module_loading: str = "EAGER"
    max_prefixes_per_campaign: int = 64
    min_target_length: int = 9
    max_target_length: int = 16
    campaign_time_slice_seconds: float = 15 * 60.0
    max_expected_campaign_seconds: float = 6 * 60 * 60.0
    default_keys_per_second: float = 10_000_000.0
    rate_ewma_alpha: float = 0.30
    retry_delay_seconds: float = 5.0
    max_consecutive_failures: int = 3
    auto_build_if_source_present: bool = True
    source_directory_name: str = "mc-keygen"
    binary_candidates: Tuple[str, ...] = (
        "mc-keygen/target/release/mc-keygen",
        "mc-keygen",
    )
    # When every feasible target has been found, the scheduler escalates the
    # difficulty budget rather than idling forever.
    escalation_factor: float = 4.0
    max_escalations: int = 6


@dataclass(frozen=True)
class BoardConfig:
    """Leaderboard sizes and the diversity rule."""

    top_repeater_ids: int = 500
    top_vanity_keys: int = 500
    top_category_keys: int = 500
    families: Tuple[str, ...] = CATEGORY_BOARD_FAMILIES
    # A key pattern is "unique" only if the board is not already full of the
    # same shape. The hall of fame keeps at most this many entries sharing one
    # canonical pattern signature (e.g. the same repeated word or run digit).
    # Set to 0 to disable the rule entirely.
    max_per_signature: int = 3
    initial_minimum_bits: float = 16.0

    @property
    def initial_minimum_score(self) -> int:
        return int(self.initial_minimum_bits * RARITY_SCORE_PER_BIT)


@dataclass(frozen=True)
class Config:
    """The complete runtime configuration."""

    paths: Paths
    cpu: CpuConfig = field(default_factory=lambda: CpuConfig().resolved())
    gpu: GpuConfig = field(default_factory=GpuConfig)
    boards: BoardConfig = field(default_factory=BoardConfig)
    node_id: str = field(default_factory=default_node_id)

    status_interval_seconds: float = 30.0
    checkpoint_interval_seconds: float = 30.0
    worker_health_interval_seconds: float = 5.0
    result_queue_size: int = 4_096
    # A copy of the previous snapshot guards against a bad write. Taking one on
    # every checkpoint doubles disk traffic for little gain, since writes are
    # already atomic, integrity-hashed, and shadowed by the history journal.
    backup_interval_seconds: float = 300.0
    urgent_checkpoint_bits: float = 28.0
    urgent_checkpoint_min_interval_seconds: float = 1.0
    max_runtime_seconds: float = 0.0
    max_worker_restarts: int = 5
    worker_restart_window_seconds: float = 300.0

    # A find is announced when it lands this high on the hall of fame. CPU
    # announcements are rate limited because the boards fill in a burst at
    # startup; a new number one always gets through.
    notable_rank: int = 10
    notable_min_interval_seconds: float = 1.0

    @classmethod
    def create(
        cls,
        data_directory: Optional[Path] = None,
        cpu_workers: Optional[int] = None,
        gpu_enabled: bool = True,
        max_runtime_seconds: float = 0.0,
        node_id: Optional[str] = None,
    ) -> "Config":
        directory = data_directory or default_data_directory()
        cpu = CpuConfig(requested_workers=cpu_workers or 0).resolved()
        return cls(
            paths=Paths.for_directory(directory),
            cpu=cpu,
            gpu=GpuConfig(enabled=gpu_enabled),
            max_runtime_seconds=max_runtime_seconds,
            node_id=node_id or default_node_id(),
        )

    @property
    def urgent_checkpoint_score(self) -> int:
        return int(self.urgent_checkpoint_bits * RARITY_SCORE_PER_BIT)

    def validate(self) -> None:
        if not NODE_ID_PATTERN.fullmatch(self.node_id):
            raise ValueError(
                "node ID must be 1-64 characters using letters, digits, '.', '_' or '-'"
            )
        if self.cpu.workers < 1:
            raise ValueError("at least one CPU worker is required")
        if min(
            self.boards.top_repeater_ids,
            self.boards.top_vanity_keys,
            self.boards.top_category_keys,
        ) < 1:
            raise ValueError("leaderboard sizes must be positive")
        if MAX_TOTAL_TIEBREAKER_BONUS >= RARITY_SCORE_PER_BIT:
            raise ValueError("all aesthetic bonuses combined must stay below one rarity bit")
        if not 6 <= self.gpu.min_target_length <= self.gpu.max_target_length <= 62:
            raise ValueError("invalid GPU target length range")
        if not 16 <= self.gpu.harvest_minimum_bits <= 256:
            raise ValueError("harvest minimum must be between 16 and 256 rarity bits")
        if self.boards.max_per_signature < 0:
            raise ValueError("max_per_signature must not be negative")


def worker_settings(config: Config) -> dict:
    """The minimal, picklable subset a CPU worker needs.

    Spawned children re-import the package with default globals, so anything a
    worker depends on must travel with it explicitly.
    """
    return {
        "worker_cpu_ids": list(config.cpu.worker_cpu_ids),
        "random_seed_batch": config.cpu.random_seed_batch,
        "pin_workers": config.cpu.pin_workers and hasattr(os, "sched_setaffinity"),
        "nice_increment": config.cpu.nice_increment,
        "counter_batch_size": config.cpu.counter_batch_size,
        "cutoff_refresh_interval": config.cpu.cutoff_refresh_interval,
    }


def describe_cpu_ids(cpu_ids: Sequence[int]) -> str:
    return ",".join(str(cpu_id) for cpu_id in cpu_ids)
