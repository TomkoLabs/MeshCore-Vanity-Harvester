"""Console presentation.

Kept apart from the supervisor loop so that what the user reads can be changed
and tested without touching what the program does.

The guiding rule: never make someone infer the state of the system from
indirect evidence. If the GPU is idle, say so and say what to do about it.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, List, Mapping, Optional, Sequence, Tuple

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _env_flag(name: str) -> Optional[bool]:
    """Read a standard env switch. "0" and "" mean off, not merely present."""
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() not in ("", "0", "false", "no")


def supports_colour(stream=None) -> bool:
    stream = stream or sys.stdout
    if _env_flag("NO_COLOR"):
        return False
    forced = _env_flag("FORCE_COLOR")
    if forced is not None:
        return forced
    return bool(getattr(stream, "isatty", lambda: False)())


class Style:
    """Colour that quietly disappears when the output is not a terminal."""

    def __init__(self, enabled: Optional[bool] = None, stream=None) -> None:
        self.enabled = supports_colour(stream) if enabled is None else enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap(BOLD, text)

    def dim(self, text: str) -> str:
        return self._wrap(DIM, text)

    def good(self, text: str) -> str:
        return self._wrap(GREEN, text)

    def warn(self, text: str) -> str:
        return self._wrap(YELLOW, text)

    def highlight(self, text: str) -> str:
        return self._wrap(CYAN, text)


# --- Formatting helpers ----------------------------------------------------


def duration(seconds: float) -> str:
    """Human durations: 45s, 12m 30s, 3h 07m, 2d 14h."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, second = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {second:02d}s"
    hours, minute = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minute:02d}m"
    days, hour = divmod(hours, 24)
    return f"{days}d {hour:02d}h"


def count(value: float) -> str:
    """Compact magnitudes: 512, 51.0k, 1.27M, 462M, 26.0B, 1.10T."""
    value = float(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= limit:
            return f"{value / limit:.3g}{suffix}"
    return f"{value:.0f}"


def rate(value: float) -> str:
    return f"{count(value)}/s"


def key_preview(public_key: str, head: int = 16) -> str:
    return public_key[:head] + "…" if len(public_key) > head else public_key


def labelled(rows: Sequence[Tuple[str, str]], indent: str = "") -> List[str]:
    if not rows:
        return []
    width = max(len(label) for label, _ in rows) + 2
    return [f"{indent}{label + ':':<{width}}{value}" for label, value in rows]


# --- Startup ---------------------------------------------------------------


def engine_block(
    style: Style,
    cpu_workers: int,
    total_cpus: int,
    backend,
    target_count: int,
) -> List[str]:
    """The answer to 'what is actually running right now'."""
    lines = [style.bold("Engines")]

    if backend.active:
        lines.append("  " + style.good(backend.headline))
    else:
        lines.append("  " + style.warn(backend.headline))
    lines.append("  " + style.dim(backend.detail))
    if backend.remedy:
        lines.append("  " + style.dim("→ " + backend.remedy))

    lines.append("")
    lines.append(f"  CPU   {cpu_workers} workers of {total_cpus} logical CPUs   "
                 + style.dim("every pattern shape, anywhere in the key"))
    if backend.active:
        device = "GPU" if backend.mode == "gpu" else "CPU"
        lines.append(f"  {device:<5} mc-keygen, {target_count:,} exact prefixes    "
                     + style.dim("exact prefixes only, far faster"))
    else:
        lines.append("  " + style.dim("mc-keygen  not running"))
    return lines


def resume_line(
    style: Style,
    recovery_source: str,
    unique_count: int,
    hall_count: int,
    attempts_before: int,
    elapsed_before: float,
    rescored: int,
) -> List[str]:
    if recovery_source == "new" or (not unique_count and not hall_count):
        return [style.dim("Starting a fresh set of leaderboards.")]

    where = {
        "private_state": "the saved leaderboards",
        "history_journal": "the recovery journal (the snapshot was unreadable)",
    }.get(recovery_source, recovery_source)

    lines = [
        style.good(f"Resuming from {where}."),
        style.dim(f"  {unique_count} repeater IDs, {hall_count} hall entries, "
                  f"{count(attempts_before)} keys tried over {duration(elapsed_before)}"),
    ]
    if rescored:
        lines.append(style.dim(f"  {rescored} saved keys re-ranked under the current scoring model"))
    return lines


# --- Running ---------------------------------------------------------------


def status_line(
    style: Style,
    elapsed: float,
    cpu_attempts: int,
    cpu_rate: float,
    unique_count: int,
    unique_limit: int,
    hall_count: int,
    hall_limit: int,
    cutoff_bits: float,
    best: Optional[Mapping[str, Any]],
    backend_summary: str,
    cpu_found: int = 0,
) -> str:
    """One line that answers "is anything happening?" for both engines.

    The CPU workers keep finding keys long after they stop being announced —
    once the hall fills with deep GPU prefixes, a good CPU find no longer
    reaches the top ten. Without a running count the CPU looks idle when it is
    not.
    """
    best_text = (
        f"{best['rarity_bits']:.1f} bits {best['repeater_id']}"
        if best else "none yet"
    )
    return (
        f"{style.dim(duration(elapsed).rjust(8))}  "
        f"CPU {count(cpu_attempts)} @ {rate(cpu_rate)} ({cpu_found} found)  ·  "
        f"{backend_summary}  ·  "
        f"boards {unique_count}/{unique_limit} IDs, {hall_count}/{hall_limit} hall  ·  "
        f"cutoff {cutoff_bits:.1f} bits  ·  "
        f"best {style.highlight(best_text)}"
    )


def backend_summary(
    active: bool,
    mode: str,
    found: int,
    total: int,
    matches: int,
    keys_per_second: float,
    active_campaign: Sequence[str],
    campaign_elapsed: float,
) -> str:
    if not active:
        return "GPU off"
    label = "GPU" if mode == "gpu" else "mc-keygen"
    if active_campaign:
        activity = f"hunting {len(active_campaign)}x{len(active_campaign[0])}-char for {duration(campaign_elapsed)}"
    else:
        activity = "idle"
    return f"{label} {rate(keys_per_second)} ({matches} found, {found}/{total} targets, {activity})"


def match_line(
    style: Style,
    record: Mapping[str, Any],
    keys_per_second: Optional[float] = None,
    rank: Optional[int] = None,
) -> str:
    """One line per notable find, from either engine, tagged with its source."""
    reason = (record.get("reasons") or ["match"])[0]
    if len(reason) > 60:
        reason = reason[:59] + "…"
    source = str(record.get("source", ""))
    tag = "GPU" if "gpu" in source else ("MCK" if "keygen" in source else "CPU")
    position = f"#{rank}" if rank else "  "
    speed = f"  ·  {rate(keys_per_second)}" if keys_per_second else ""
    return (
        style.good(f"  FOUND {tag} ")
        + style.dim(f"{position:>4} ")
        # The whole key, not a preview: this is the line people copy from.
        + style.bold(str(record["public_key"]))
        + f"  {float(record['rarity_bits']):.2f} bits  ·  {reason}{speed}"
    )


class RestoreProgress:
    """Report a slow restore as work in progress rather than a silent pause.

    Verifying a key that carries no seed costs about a tenth of a second, so a
    board full of externally supplied keys can take minutes. Saying nothing for
    that long is indistinguishable from a hang.
    """

    def __init__(self, style: Style, stream=None, announce_threshold: int = 200) -> None:
        self.style = style
        self.stream = stream or sys.stderr
        self.announce_threshold = announce_threshold
        self.announced = False
        self.total = 0
        # Start the clock now, so a restore that finishes quickly prints its
        # headline and its result without a pointless "1/1656" in between.
        self.last_shown = time.monotonic()

    def __call__(self, phase: str, first: int, second: int) -> None:
        if phase == "start":
            self.total = first
            slow = second
            # Estimate: the slow path dominates whenever it is used at all.
            if slow or first >= self.announce_threshold:
                self.announced = True
                detail = f" ({slow} need full re-derivation)" if slow else ""
                print(self.style.dim(f"  Verifying {first} saved keys{detail}…"),
                      file=self.stream, flush=True)
        elif phase == "step" and self.announced:
            now = time.monotonic()
            if now - self.last_shown >= 2.0 and second:
                self.last_shown = now
                print(self.style.dim(f"    {first}/{second} verified…"),
                      file=self.stream, flush=True)
        elif phase == "done" and self.announced:
            print(self.style.dim(f"  Verified {first} saved keys."),
                  file=self.stream, flush=True)


# --- Leaderboard rendering -------------------------------------------------


def leaderboard_table(
    style: Style,
    records: Sequence[Mapping[str, Any]],
    limit: int = 20,
    show_reason: bool = True,
) -> List[str]:
    """Boards are ordered by score, so show score alongside rarity.

    Score is rarity plus up to 0.95 of a bit of pattern-quality preference.
    Showing only rarity makes the ordering look wrong whenever two keys sit
    less than a bit apart, which is exactly when the tie-breaker decides.
    """
    if not records:
        return [style.dim("  (no entries yet)")]

    lines = [
        style.dim(f"  {'#':>3}  {'repeater':<8}  {'rarity':>7}  {'rank':>6}  "
                  f"{'public key':<24}  reason"),
    ]
    for index, record in enumerate(records[:limit], 1):
        reason = (record.get("reasons") or [""])[0] if show_reason else ""
        if len(reason) > 50:
            reason = reason[:49] + "…"
        rarity = float(record.get("rarity_bits", 0.0))
        rank_value = int(record.get("score", 0)) / 1_000_000
        lines.append(
            f"  {index:>3}  {str(record['repeater_id']):<8}  "
            f"{rarity:>6.2f}b  {rank_value:>6.2f}  "
            f"{key_preview(str(record['public_key']), 22):<24}  "
            + style.dim(reason)
        )
    if len(records) > limit:
        lines.append(style.dim(f"  … and {len(records) - limit} more"))
    return lines


def ranking_footnote(style: Style) -> str:
    return style.dim(
        "  rarity = bits of surprise (each +4 bits is 16x rarer).  "
        "rank = rarity plus up to 0.95 for pattern quality."
    )


def summary_block(
    style: Style,
    attempts_run: int,
    attempts_total: int,
    elapsed_run: float,
    elapsed_total: float,
    backend_stats: Optional[Mapping[str, Any]],
    hall: Sequence[Mapping[str, Any]],
    diversity: float,
    paths,
) -> List[str]:
    lines = ["", style.bold("This run")]
    lines.extend(labelled([
        ("CPU keys tried", f"{count(attempts_run)} in {duration(elapsed_run)}"),
    ], indent="  "))
    if backend_stats:
        lines.extend(labelled([
            ("mc-keygen", f"{backend_stats['matches']} matches, "
                          f"{backend_stats['found']}/{backend_stats['total']} targets satisfied, "
                          f"{rate(backend_stats['keys_per_second'])}"),
        ], indent="  "))

    lines.extend(["", style.bold("All time")])
    lines.extend(labelled([
        ("CPU keys tried", f"{count(attempts_total)} over {duration(elapsed_total)}"),
        ("Board diversity", f"{diversity:.0%} distinct pattern shapes"),
    ], indent="  "))

    if hall:
        lines.extend(["", style.bold("Best identities so far")])
        lines.extend(leaderboard_table(style, hall, limit=5))
        lines.append(ranking_footnote(style))

    lines.extend(["", style.bold("Where your results are")])
    lines.extend(labelled([
        ("Browse these", str(paths.public_id_list)),
        ("Full boards", str(paths.public_state)),
        ("Private keys", str(paths.private_key_map) + style.warn("  ← secret, mode 0600")),
    ], indent="  "))
    lines.append("")
    lines.append(style.dim("  Look a key up: find its public_key in the index, then in the private map."))
    lines.append(style.dim("  See the boards any time without harvesting:  ./run.sh --top 20"))
    return lines
