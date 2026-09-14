"""Approximate prefix rarity, after a visible six-character ID gate.

Every scored feature starts at character zero. Longer continuations earn
rarity according to how many hex digits they constrain, with a charge for the
number of equally acceptable patterns. A freely chosen repeat unit, for
example, earns credit only for its matching continuation.

All aesthetic bonuses combined remain below one rarity bit. Curated exact
prefixes use their own reference-class count when they are the primary feature;
personal preference itself adds only a capped aesthetic bonus.

Family corrections inherited from the previous all-position model are
conservative starting estimates for this narrower search. Rarity is not an
exact probability for the combined leaderboard. ``scripts/calibrate.py``
measures the remaining error where a random sample provides enough evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .catalog import (
    HEX_DIGITS,
    HEX_WORDS,
    LOCAL_EXACT_PREFIXES,
    PREFERRED_EXACT_PREFIXES,
    WORDS_BY_FIRST,
    chain_count,
    words_of_length,
)
from .config import (
    MAX_PRIMARY_SUBJECTIVE_BONUS,
    MAX_SECONDARY_BONUS,
    MAX_TOTAL_TIEBREAKER_BONUS,
    PUBLIC_KEY_HEX_LENGTH,
    RARITY_SCORE_PER_BIT,
    REPEATER_ID_HEX_LENGTH,
)

BITS_PER_NIBBLE = 4.0
MIN_RUN_LENGTH = 4
MIN_SEQUENCE_LENGTH = 5
MIN_PALINDROME_LENGTH = 6
MIN_PERIODIC_LENGTH = 6
MAX_PERIODIC_UNIT = 12

# Residual corrections in bits, subtracted from the structural estimate.
# Positive means "the structural formula is optimistic for this family".
FAMILY_CALIBRATION_BITS: Mapping[str, float] = {
    "word": 0.85,
    "single_run": 0.0,
    "periodic": 2.1,
    # Negative: overlapping sequence windows are strongly correlated, so the
    # structural formula slightly understates how rare a long sequence is.
    "sequence": -0.2,
    "palindrome": 1.4,
    "special": 0.0,
}

_ALL_PREFERENCES: Dict[str, Tuple[int, str]] = {}
for _source in (PREFERRED_EXACT_PREFIXES, LOCAL_EXACT_PREFIXES):
    for _prefix, (_quality, _label) in _source.items():
        _existing = _ALL_PREFERENCES.get(_prefix)
        if _existing is None or _quality > _existing[0]:
            _ALL_PREFERENCES[_prefix] = (_quality, _label)

_PREFERENCES_BY_LENGTH: Dict[int, int] = {}
for _prefix in _ALL_PREFERENCES:
    _PREFERENCES_BY_LENGTH[len(_prefix)] = _PREFERENCES_BY_LENGTH.get(len(_prefix), 0) + 1

_PREFERENCE_LENGTHS: Tuple[int, ...] = tuple(sorted({len(p) for p in _ALL_PREFERENCES}, reverse=True))


@dataclass(frozen=True)
class PatternFeature:
    """One reason a key might be interesting."""

    kind: str
    start: int
    length: int
    rarity_bits: float
    semantic_bonus: int
    description: str
    signature: str = ""


_FAMILY_BY_KIND: Mapping[str, str] = {
    "word": "word",
    "word_repeat": "word",
    "word_extension": "word",
    "compound_words": "word",
    "compound_extension": "word",
    "single_run": "single_run",
    "periodic": "periodic",
    "structured_id": "periodic",
    "sequence": "sequence",
    "palindrome": "palindrome",
    "preference": "special",
}


def pattern_family(kind: str) -> str:
    return _FAMILY_BY_KIND.get(kind, "special")


# --- The one rarity formula every family goes through ----------------------


def rarity_bits(
    kind: str,
    constrained_nibbles: float,
    alternatives: int = 1,
    positions: int = 1,
) -> float:
    """Bits of surprise, after charging for reference class and position."""
    bits = (
        BITS_PER_NIBBLE * constrained_nibbles
        - math.log2(max(1, alternatives))
        - math.log2(max(1, positions))
        - FAMILY_CALIBRATION_BITS.get(pattern_family(kind), 0.0)
    )
    return max(0.0, bits)


# --- Aesthetic tie-breakers (always capped below one rarity bit) -----------


def _position_bonus(start: int, length: int) -> int:
    if start == 0:
        return 330_000 + min(120_000, max(0, length - 6) * 8_000)
    if start < REPEATER_ID_HEX_LENGTH:
        return max(100_000, 240_000 - start * 28_000)
    return max(0, 75_000 - start * 2_500)


_KIND_BONUS: Mapping[str, int] = {
    "preference": 190_000,
    "word_repeat": 150_000,
    "word_extension": 145_000,
    "compound_words": 140_000,
    "compound_extension": 145_000,
    "word": 125_000,
    "single_run": 250_000,
    "periodic": 180_000,
    "sequence": 100_000,
    "palindrome": 90_000,
    "structured_id": 100_000,
}


def _feature_subjective_bonus(feature: PatternFeature, preference_bonus: int) -> int:
    kind_bonus = _KIND_BONUS.get(feature.kind, 30_000)
    extra = preference_bonus if feature.start == 0 else 0
    return min(
        MAX_PRIMARY_SUBJECTIVE_BONUS,
        max(0, feature.semantic_bonus)
        + kind_bonus
        + _position_bonus(feature.start, feature.length)
        + extra,
    )


def feature_score(feature: PatternFeature, preference_bonus: int = 0) -> int:
    return int(round(feature.rarity_bits * RARITY_SCORE_PER_BIT)) + _feature_subjective_bonus(
        feature, preference_bonus
    )


# --- Primitive pattern measurements ---------------------------------------


def repeated_match_length(value: str, start: int, unit: str) -> int:
    if not unit or start >= len(value):
        return 0
    index = start
    while index < len(value) and value[index] == unit[(index - start) % len(unit)]:
        index += 1
    return index - start


def same_run_length(value: str, start: int) -> int:
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


def _sequence_alternatives(length: int) -> int:
    """Ascending and descending, times the start digits that actually fit.

    A run of L ascending digits cannot start above 16-L, so the leading nibble
    is worth less than a free choice. The old model assumed all 16 worked and
    therefore *understated* sequence rarity.
    """
    viable_starts = max(1, 17 - length)
    return 2 * viable_starts


def _longest_prefix_palindrome(value: str, minimum: int = MIN_PALINDROME_LENGTH) -> int:
    for length in range(len(value), minimum - 1, -1):
        prefix = value[:length]
        if prefix == prefix[::-1]:
            return length
    return 0


def _word_chains(value: str) -> List[Tuple[int, Tuple[str, ...]]]:
    """All prefix endpoints segmentable into at least two catalog words.

    Keep intermediate endpoints: a shorter chain can earn a longer final run.
    """
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
    return sorted(candidates, reverse=True)


def _compound_features(value: str) -> List[PatternFeature]:
    features: List[PatternFeature] = []
    for length, chain in _word_chains(value):
        if length < MIN_PERIODIC_LENGTH:
            continue
        quality = min(300_000, 65_000 + sum(HEX_WORDS[word][0] for word in chain) // len(chain))
        signature = "compound:" + "+".join(chain)
        label = " + ".join(chain)
        features.append(PatternFeature(
            "compound_words", 0, length,
            rarity_bits("compound_words", length, chain_count(length), 1),
            quality, f"compound vanity prefix {label} spans {length} characters", signature,
        ))
        end = length
        while end < len(value) and value[end] == value[length - 1]:
            end += 1
        if end > length:
            # Charge for choosing where the words stop and the run begins.
            # There are at most 58 eligible base lengths in a 64-nibble key.
            alternatives = chain_count(length) * (PUBLIC_KEY_HEX_LENGTH - MIN_PERIODIC_LENGTH)
            features.append(PatternFeature(
                "compound_extension", 0, end,
                rarity_bits("compound_extension", end, alternatives, 1),
                quality,
                f"compound prefix {label} extends with {end - length} additional '{value[length - 1]}' characters",
                signature,
            ))
    return features


def _structured_id_features(repeater_id: str) -> List[PatternFeature]:
    """Shapes that are visible in the six-character repeater ID itself."""
    features: List[PatternFeature] = []
    a, b, c, d, e, f = repeater_id
    if a == b == c and d == e == f:
        features.append(PatternFeature(
            "structured_id", 0, 6, rarity_bits("structured_id", 4), 35_000,
            "AAABBB repeater ID", "structured:AAABBB",
        ))
    if a == b and c == d and e == f:
        features.append(PatternFeature(
            "structured_id", 0, 6, rarity_bits("structured_id", 3), 30_000,
            "AABBCC repeater ID", "structured:AABBCC",
        ))
    if repeater_id == repeater_id[::-1]:
        features.append(PatternFeature(
            "palindrome", 0, 6, rarity_bits("palindrome", 3), 35_000,
            "six-character palindromic repeater ID", "palindrome:6",
        ))
    return features


def _preference_match(public_key_hex: str) -> Tuple[int, Optional[Tuple[str, int, str]]]:
    """Best curated-preference hit: (aesthetic bonus, (prefix, quality, label))."""
    best: Optional[Tuple[str, int, str]] = None
    for prefix, (quality, label) in _ALL_PREFERENCES.items():
        if public_key_hex.startswith(prefix):
            if best is None or (len(prefix), quality) > (len(best[0]), best[1]):
                best = (prefix, quality, label)
    if best is None:
        return 0, None
    return best[1], best


# --- Full analysis ---------------------------------------------------------


def _empty_analysis() -> Dict[str, Any]:
    return {
        "score": 0, "reasons": [], "rarity_bits": 0.0,
        "pattern_length": 0, "pattern_start": 0, "pattern_kind": "none",
        "pattern_family": "none", "pattern_signature": "none",
    }


def analyze_public_key(public_key_hex: str) -> Dict[str, Any]:
    if len(public_key_hex) != PUBLIC_KEY_HEX_LENGTH or any(c not in HEX_DIGITS for c in public_key_hex):
        raise ValueError("public key must contain exactly 64 uppercase hexadecimal characters")

    if not has_desirable_id(public_key_hex):
        return _empty_analysis()

    features: List[PatternFeature] = []
    repeater_id = public_key_hex[:REPEATER_ID_HEX_LENGTH]
    preference_bonus, preference = _preference_match(public_key_hex)

    # Every feature starts at character zero. A disconnected tail earns no credit.
    prefix_words = [word for word in _PREFIX_WORDS_BY_TRIPLE.get(public_key_hex[:3], ())
                    if public_key_hex.startswith(word)]
    for word in prefix_words:
        quality, description = HEX_WORDS[word]
        features.append(PatternFeature(
            "word", 0, len(word),
            rarity_bits("word", len(word), words_of_length(len(word)), 1),
            quality, f"vanity word '{word}' ({description})", f"word:{word}",
        ))

    # -- prefix words that repeat or extend --------------------------------
    for word in prefix_words:
        quality, description = HEX_WORDS[word]
        alternatives = words_of_length(len(word))

        repeated_length = repeated_match_length(public_key_hex, 0, word)
        if repeated_length >= len(word) * 2:
            features.append(PatternFeature(
                "word_repeat", 0, repeated_length,
                rarity_bits("word_repeat", repeated_length, alternatives, 1),
                min(300_000, quality + 100_000),
                f"known word '{word}' repeats for {repeated_length} characters ({description})",
                f"word:{word}",
            ))

        index = len(word)
        while index < len(public_key_hex) and public_key_hex[index] == word[-1]:
            index += 1
        if index > len(word):
            features.append(PatternFeature(
                "word_extension", 0, index,
                rarity_bits("word_extension", index, alternatives, 1),
                min(285_000, quality + 90_000),
                f"word '{word}' extends with {index - len(word)} additional '{word[-1]}' characters",
                f"word:{word}",
            ))

    # -- compound words at the prefix --------------------------------------
    features.extend(_compound_features(public_key_hex))

    # -- anchored runs and nonwrapping sequences ----------------------------
    run_length = same_run_length(public_key_hex, 0)
    if run_length >= MIN_RUN_LENGTH:
        features.append(PatternFeature(
            "single_run", 0, run_length,
            rarity_bits("single_run", run_length - 1), 15_000,
            f"leading '{public_key_hex[0]}' run of {run_length} characters",
            f"run:{public_key_hex[0]}",
        ))
    for step, label in ((1, "ascending"), (-1, "descending")):
        length = _sequence_length(public_key_hex, 0, step)
        if length >= MIN_SEQUENCE_LENGTH:
            features.append(PatternFeature(
                "sequence", 0, length,
                rarity_bits("sequence", length, _sequence_alternatives(length)), 20_000,
                f"{length}-character leading {label} sequence", f"sequence:{label}",
            ))

    # -- generic periodic prefixes, including partial final units -----------
    for unit_length in range(2, MAX_PERIODIC_UNIT + 1):
        unit = public_key_hex[:unit_length]
        if not public_key_hex.startswith(unit, unit_length):
            continue
        length = repeated_match_length(public_key_hex, 0, unit)
        if length >= max(MIN_PERIODIC_LENGTH, unit_length * 2):
            features.append(PatternFeature(
                "periodic", 0, length, rarity_bits("periodic", length - unit_length),
                max(0, 50_000 - unit_length * 2_500),
                f"leading unit '{unit}' repeats for {length} characters", f"periodic:{unit}",
            ))

    # Only an anchored palindrome behind an already desirable ID can qualify.
    length = _longest_prefix_palindrome(public_key_hex)
    if length:
        features.append(PatternFeature(
            "palindrome", 0, length, rarity_bits("palindrome", length // 2), 25_000,
            f"{length}-character leading palindrome", f"palindrome:{length}",
        ))

    features.extend(_structured_id_features(repeater_id))

    # -- curated preference fallback ---------------------------------------
    #
    # A preference never buys rarity beyond its own reference class: hitting one
    # of N pre-committed prefixes of this length is 4L - log2(N) bits. This only
    # becomes the primary feature when no generic family already covers the span.
    if preference is not None:
        prefix, quality, label = preference
        features.append(PatternFeature(
            "preference", 0, len(prefix),
            rarity_bits("preference", len(prefix), _PREFERENCES_BY_LENGTH.get(len(prefix), 1), 1),
            quality, f"curated preference '{prefix}' ({label})", f"preference:{prefix}",
        ))

    if not features:
        return _empty_analysis()

    scored = sorted(
        ((feature_score(feature, preference_bonus), feature) for feature in features),
        key=lambda item: (-item[0], -item[1].length, item[1].start, item[1].description),
    )
    primary_score, primary = scored[0]

    # Secondary reasons add deterministic sub-bit ordering. Deduplicated by
    # (kind, start) so overlapping views of one pattern cannot stack.
    secondary_bonus = 0
    secondary_reasons: List[str] = []
    seen: set = {(primary.kind, primary.start)}
    seen_signatures = {primary.signature}
    for _candidate_score, feature in scored[1:]:
        marker = (feature.kind, feature.start)
        if marker in seen or feature.signature in seen_signatures:
            continue
        contribution = min(90_000, max(10_000, int(feature.rarity_bits * 1_500)))
        contribution = min(contribution, MAX_SECONDARY_BONUS - secondary_bonus)
        if contribution <= 0:
            break
        secondary_bonus += contribution
        seen.add(marker)
        seen_signatures.add(feature.signature)
        secondary_reasons.append(feature.description)
        if len(secondary_reasons) >= 5 or secondary_bonus >= MAX_SECONDARY_BONUS:
            break

    reasons = [
        primary.description,
        f"estimated rarity: {primary.rarity_bits:.2f} bits; "
        f"pattern length {primary.length}/{PUBLIC_KEY_HEX_LENGTH}; offset {primary.start}",
        *secondary_reasons,
    ]
    if preference is not None and primary.kind != "preference":
        reasons.append(f"matches curated preference '{preference[0]}' ({preference[2]})")

    return {
        "score": int(primary_score + secondary_bonus),
        "reasons": reasons[:8],
        "rarity_bits": round(primary.rarity_bits, 3),
        "pattern_length": int(primary.length),
        "pattern_start": int(primary.start),
        "pattern_kind": primary.kind,
        "pattern_family": pattern_family(primary.kind),
        "pattern_signature": primary.signature or primary.kind,
    }


def score_public_key(public_key_hex: str) -> Tuple[int, List[str]]:
    analysis = analyze_public_key(public_key_hex)
    return int(analysis["score"]), list(analysis["reasons"])


# --- Fast pre-filter -------------------------------------------------------
#
# Contract: quick_candidate() must never reject a key whose full score reaches
# the cutoff. False positives merely cost a full analysis; a false negative
# would silently and permanently hide a leaderboard-worthy key.
#
# The visible-ID gate rejects almost every key before longer measurements.
# Everything past it uses the same rarity formula as the full scorer.

_SEQUENCE_STRINGS = (
    ["0123456789ABCDEF"[i:i + MIN_SEQUENCE_LENGTH] for i in range(17 - MIN_SEQUENCE_LENGTH)]
    + ["FEDCBA9876543210"[i:i + MIN_SEQUENCE_LENGTH] for i in range(17 - MIN_SEQUENCE_LENGTH)]
)

# Words are at least three characters, so the key's first three characters
# decide the entire candidate set for a prefix word. Almost every key misses on
# a single dict lookup instead of a string comparison per catalog entry.
_PREFIX_WORDS_BY_TRIPLE: Dict[str, Tuple[str, ...]] = {}
for _word in HEX_WORDS:
    _PREFIX_WORDS_BY_TRIPLE.setdefault(_word[:3], ())
    _PREFIX_WORDS_BY_TRIPLE[_word[:3]] += (_word,)

# Visible words may be shorter than the ID (DEAD) or extend beyond it
# (DEADBE is the visible part of DEADBEEF). This is eligibility, not a bonus.
_VISIBLE_PREFIXES = frozenset(prefix[:REPEATER_ID_HEX_LENGTH]
                             for prefix in (*HEX_WORDS, *_ALL_PREFERENCES))
_VISIBLE_PREFIX_LENGTHS = tuple(sorted({len(prefix) for prefix in _VISIBLE_PREFIXES}))
_VISIBLE_SEQUENCE_PREFIXES = frozenset(_SEQUENCE_STRINGS)


def has_desirable_id(value: str) -> bool:
    """Require a recognizable pattern within the first six hex characters.

    Four leading equal digits, five ordered digits, a catalog/curated prefix,
    ABABAB/ABCABC, AAABBB, AABBCC or a six-character palindrome qualify.
    Longer patterns are measured only after passing this gate.
    """
    a, b, c, d, e, f = value[:REPEATER_ID_HEX_LENGTH]
    if a == b == c == d or value[:5] in _VISIBLE_SEQUENCE_PREFIXES:
        return True
    if ((a == c == e and b == d == f) or (a == d and b == e and c == f)
            or (a == b == c and d == e == f) or (a == b and c == d and e == f)
            or (a == f and b == e and c == d)):
        return True
    return any(value[:length] in _VISIBLE_PREFIXES for length in _VISIBLE_PREFIX_LENGTHS)


_MAX_BONUS_BITS = MAX_TOTAL_TIEBREAKER_BONUS / RARITY_SCORE_PER_BIT

# A score is rounded to a whole number of score units before it becomes a
# cutoff, so a threshold derived back from it can land a fraction of a unit
# above the rarity that produced it. Without this slack the filter would reject
# the very key that set the cutoff. One hundred-thousandth of a bit is far
# below any real discrimination and removes the boundary case entirely.
RARITY_COMPARISON_EPSILON = 1e-5


def required_rarity_bits(cutoff_score: int) -> float:
    return cutoff_score / RARITY_SCORE_PER_BIT - _MAX_BONUS_BITS - RARITY_COMPARISON_EPSILON


def quick_candidate(public_key_hex: str, cutoff_score: int) -> bool:
    """Conservative prefix screen; every qualifying full score must survive."""
    required = required_rarity_bits(cutoff_score)
    if required <= 0:
        return True
    if not has_desirable_id(public_key_hex):
        return False

    # A cheap visible gate excludes almost every random key. For survivors,
    # measure only the anchored candidates, using the scorer's rarity formula.
    for length in _PREFERENCE_LENGTHS:
        if (public_key_hex[:length] in _ALL_PREFERENCES
                and rarity_bits("preference", length, _PREFERENCES_BY_LENGTH[length]) >= required):
            return True
    if any(feature.rarity_bits >= required
           for feature in _structured_id_features(public_key_hex[:6])):
        return True
    length = same_run_length(public_key_hex, 0)
    if length >= MIN_RUN_LENGTH and rarity_bits("single_run", length - 1) >= required:
        return True
    for step in (1, -1):
        length = _sequence_length(public_key_hex, 0, step)
        if (length >= MIN_SEQUENCE_LENGTH
                and rarity_bits("sequence", length, _sequence_alternatives(length)) >= required):
            return True
    prefix_words = [word for word in _PREFIX_WORDS_BY_TRIPLE.get(public_key_hex[:3], ())
                    if public_key_hex.startswith(word)]
    for word in prefix_words:
        alternatives = words_of_length(len(word))
        if rarity_bits("word", len(word), alternatives) >= required:
            return True
        repeated = repeated_match_length(public_key_hex, 0, word)
        if repeated >= 2 * len(word) and rarity_bits("word_repeat", repeated, alternatives) >= required:
            return True
        extension = len(word)
        while extension < len(public_key_hex) and public_key_hex[extension] == word[-1]:
            extension += 1
        if extension > len(word) and rarity_bits("word_extension", extension, alternatives) >= required:
            return True
    if prefix_words and any(feature.rarity_bits >= required for feature in _compound_features(public_key_hex)):
        return True
    for unit_length in range(2, MAX_PERIODIC_UNIT + 1):
        unit = public_key_hex[:unit_length]
        if not public_key_hex.startswith(unit, unit_length):
            continue
        length = repeated_match_length(public_key_hex, 0, unit)
        if (length >= max(MIN_PERIODIC_LENGTH, 2 * unit_length)
                and rarity_bits("periodic", length - unit_length) >= required):
            return True
    length = _longest_prefix_palindrome(public_key_hex)
    return bool(length and rarity_bits("palindrome", length // 2) >= required)
