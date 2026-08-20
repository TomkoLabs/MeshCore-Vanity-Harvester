"""The calibrated rarity model and the fast pre-filter.

What a "rarity bit" means here
------------------------------
One bit is one halving of the probability that a *random* MeshCore public key
shows a pattern at least this good. Every family is measured against the same
open-ended question:

    "How surprised should someone be by this key, if they did not know in
     advance what we were hunting for?"

That framing forces a correction the previous model was missing. A pattern is
only as surprising as its *reference class* is small, so each feature is
charged for the alternatives that would have pleased us just as much::

    rarity_bits = 4 x constrained_nibbles
                  - log2(number of equally acceptable alternative patterns)
                  - log2(number of positions the pattern could have occupied)

Concretely: a six-character catalog word is not 24 bits of surprise, because
the catalog holds many six-character words and the key offers many positions
to host one. Charging for both is what makes a word comparable to a run, a
period, a sequence and a palindrome on one axis.

Curated preferences (Montreal 514, all-4 runs, iconic hexspeak) deliberately do
*not* buy rarity. Wanting a pattern cannot make it mathematically rarer, so a
preference contributes only to the capped aesthetic tie-breaker. That keeps the
headline invariant honest: one extra rarity bit always outranks every aesthetic
bonus combined, and the bits being compared now measure the same thing.

The residual per-family constants in ``FAMILY_CALIBRATION_BITS`` absorb the
"maximum over many overlapping chances" effect that no closed form captures
cleanly. They were fitted by measuring the observed frequency of each family
against its claim over millions of random keys; ``scripts/calibrate.py``
reproduces the measurement and reports the remaining error.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .catalog import (
    HEX_DIGITS,
    HEX_WORDS,
    LOCAL_EXACT_PREFIXES,
    PREFERRED_EXACT_PREFIXES,
    WORD_LENGTHS_DESC,
    WORD_REGEX_BY_LENGTH,
    WORD_SETS_BY_LENGTH,
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

# Palindromes are found by expanding around centres, and a 64-character string
# has 2n-1 of them, not n-L+1 start positions. Using the start count overstated
# palindrome rarity by a measurable ~1.2 bits.
PALINDROME_CENTRES = 2 * PUBLIC_KEY_HEX_LENGTH - 1

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


def _positions(length: int, start: int) -> int:
    """How many places an equally good pattern of this length could have sat."""
    if start == 0:
        return 1
    return max(1, PUBLIC_KEY_HEX_LENGTH - length + 1)


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


def _longest_palindromes(value: str, minimum_length: int = MIN_PALINDROME_LENGTH) -> List[Tuple[int, int]]:
    best_by_start: Dict[int, int] = {}
    n = len(value)
    for centre in range(n):
        for left, right in ((centre, centre), (centre, centre + 1)):
            while left >= 0 and right < n and value[left] == value[right]:
                length = right - left + 1
                if length >= minimum_length:
                    best_by_start[left] = max(best_by_start.get(left, 0), length)
                left -= 1
                right += 1
    return sorted(best_by_start.items(), key=lambda item: (-item[1], item[0]))[:4]


def _word_chain(value: str) -> Tuple[int, Tuple[str, ...]]:
    """Longest prefix segmentable into at least two catalog words."""
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


def analyze_public_key(public_key_hex: str) -> Dict[str, Any]:
    if len(public_key_hex) != PUBLIC_KEY_HEX_LENGTH or any(c not in HEX_DIGITS for c in public_key_hex):
        raise ValueError("public key must contain exactly 64 uppercase hexadecimal characters")

    features: List[PatternFeature] = []
    repeater_id = public_key_hex[:REPEATER_ID_HEX_LENGTH]
    preference_bonus, preference = _preference_match(public_key_hex)

    # -- catalog words anywhere -------------------------------------------
    prefix_words: List[str] = []
    for word, (quality, description) in HEX_WORDS.items():
        search_start = 0
        while True:
            position = public_key_hex.find(word, search_start)
            if position < 0:
                break
            features.append(PatternFeature(
                "word", position, len(word),
                rarity_bits("word", len(word), words_of_length(len(word)), _positions(len(word), position)),
                quality, f"vanity word '{word}' ({description})", f"word:{word}",
            ))
            if position == 0:
                prefix_words.append(word)
            search_start = position + 1

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
    chain_length, chain = _word_chain(public_key_hex)
    if chain_length >= MIN_PERIODIC_LENGTH:
        features.append(PatternFeature(
            "compound_words", 0, chain_length,
            rarity_bits("compound_words", chain_length, chain_count(chain_length), 1),
            min(300_000, 65_000 + sum(HEX_WORDS[word][0] for word in chain) // max(1, len(chain))),
            f"compound vanity prefix {' + '.join(chain)} spans {chain_length} characters",
            "compound:" + "+".join(chain),
        ))

    # -- identical-character runs ------------------------------------------
    index = 0
    while index < len(public_key_hex):
        run_length = same_run_length(public_key_hex, index)
        if run_length >= MIN_RUN_LENGTH:
            features.append(PatternFeature(
                "single_run", index, run_length,
                # The leading nibble is free: that freedom *is* the 16 digits.
                rarity_bits("single_run", run_length - 1, 1, _positions(run_length, index)),
                15_000,
                f"'{public_key_hex[index]}' run of {run_length} characters at offset {index}",
                f"run:{public_key_hex[index]}",
            ))
        index += run_length

    # -- ascending / descending sequences ----------------------------------
    for start in range(len(public_key_hex) - MIN_SEQUENCE_LENGTH + 1):
        for step, label in ((1, "ascending"), (-1, "descending")):
            length = _sequence_length(public_key_hex, start, step)
            if length >= MIN_SEQUENCE_LENGTH:
                features.append(PatternFeature(
                    "sequence", start, length,
                    rarity_bits("sequence", length, _sequence_alternatives(length), _positions(length, start)),
                    20_000,
                    f"{length}-character {label} sequence at offset {start}",
                    f"sequence:{label}",
                ))

    # -- generic periodic units --------------------------------------------
    for start in range(len(public_key_hex) - MIN_PERIODIC_LENGTH + 1):
        remaining = len(public_key_hex) - start
        # Unit length one is already covered by single_run.
        for unit_length in range(2, min(MAX_PERIODIC_UNIT, remaining // 2) + 1):
            unit = public_key_hex[start:start + unit_length]
            repeated_length = repeated_match_length(public_key_hex, start, unit)
            if repeated_length < max(MIN_PERIODIC_LENGTH, unit_length * 2):
                continue
            features.append(PatternFeature(
                "periodic", start, repeated_length,
                # The first unit is free; every later matching nibble is earned.
                rarity_bits("periodic", repeated_length - unit_length, 1, _positions(repeated_length, start)),
                max(0, 50_000 - unit_length * 2_500),
                f"unit '{unit}' repeats for {repeated_length} characters at offset {start}",
                f"periodic:{unit}",
            ))

    # -- palindromes --------------------------------------------------------
    for start, length in _longest_palindromes(public_key_hex):
        features.append(PatternFeature(
            "palindrome", start, length,
            rarity_bits("palindrome", length // 2, 1, 1 if start == 0 else PALINDROME_CENTRES),
            25_000,
            f"{length}-character palindrome at offset {start}",
            f"palindrome:{length}",
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
        return {
            "score": 0, "reasons": [], "rarity_bits": 0.0,
            "pattern_length": 0, "pattern_start": 0, "pattern_kind": "none",
            "pattern_family": "none", "pattern_signature": "none",
        }

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
# One combined screening regex rejects the overwhelming majority of keys in a
# single C-level pass. Everything past the screen is checked against the same
# rarity formula the full scorer uses, so the two cannot drift apart.

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

_QUICK_RUN_REGEX = re.compile(r"([0-9A-F])\1{%d,}" % (MIN_RUN_LENGTH - 1))
_QUICK_PERIODIC_REGEX = re.compile(r"(?:([0-9A-F]{2})\1{2}|([0-9A-F]{3,%d})\2)" % MAX_PERIODIC_UNIT)
_QUICK_PALINDROME_REGEX = re.compile(
    r"(?:([0-9A-F])([0-9A-F])([0-9A-F])\3\2\1|([0-9A-F])([0-9A-F])([0-9A-F])[0-9A-F]\6\5\4)"
)
_QUICK_SEQUENCE_REGEX = re.compile("|".join(_SEQUENCE_STRINGS))

# Rarity a pattern must reach before any aesthetic help could lift it to the
# cutoff. Derived, never floored: flooring it was what let qualifying keys slip
# through the old filter whenever the boards were not yet full.
# Per-length word thresholds, computed once. The full scorer and the filter
# therefore cannot drift apart: both come from rarity_bits().
_WORD_THRESHOLDS: Tuple[Tuple[int, float, float], ...] = tuple(
    (
        length,
        rarity_bits("word", length, words_of_length(length), 1),
        rarity_bits("word", length, words_of_length(length), PUBLIC_KEY_HEX_LENGTH - length + 1),
    )
    for length in WORD_LENGTHS_DESC
)


def _min_periodic_length(required: float) -> int:
    """Shortest repeated span that could possibly reach the cutoff."""
    best_case = rarity_bits("periodic", 1, 1, 1)  # bits earned per constrained nibble
    per_nibble = BITS_PER_NIBBLE
    overhead = BITS_PER_NIBBLE - best_case  # calibration charge
    return int(math.ceil((required + overhead) / per_nibble)) + 2


def _min_palindrome_length(required: float) -> int:
    """Shortest palindrome that could possibly reach the cutoff."""
    overhead = BITS_PER_NIBBLE - rarity_bits("palindrome", 1, 1, 1)
    return 2 * int(math.ceil((required + overhead) / BITS_PER_NIBBLE))


_MAX_BONUS_BITS = MAX_TOTAL_TIEBREAKER_BONUS / RARITY_SCORE_PER_BIT

# A score is rounded to a whole number of score units before it becomes a
# cutoff, so a threshold derived back from it can land a fraction of a unit
# above the rarity that produced it. Without this slack the filter would reject
# the very key that set the cutoff. One hundred-thousandth of a bit is far
# below any real discrimination and removes the boundary case entirely.
RARITY_COMPARISON_EPSILON = 1e-5


def required_rarity_bits(cutoff_score: int) -> float:
    return cutoff_score / RARITY_SCORE_PER_BIT - _MAX_BONUS_BITS - RARITY_COMPARISON_EPSILON


def _structured_or_preferred(public_key_hex: str, repeater_id: str) -> bool:
    a, b, c, d, e, f = repeater_id
    if (a == b == c and d == e == f) or (a == b and c == d and e == f):
        return True
    if repeater_id == repeater_id[::-1]:
        return True
    for length in _PREFERENCE_LENGTHS:
        if public_key_hex[:length] in _ALL_PREFERENCES:
            return True
    return False


def quick_candidate(public_key_hex: str, cutoff_score: int) -> bool:
    """True if this key could still reach the cutoff. Never a false negative.

    Checks run cheapest-and-most-selective first and are all cutoff-aware, so a
    high cutoff rejects most keys after only a dict lookup and one small regex.
    """
    required = required_rarity_bits(cutoff_score)
    if required <= 0.0:
        # Any feature at all could clear the cutoff on bonuses alone.
        return True

    repeater_id = public_key_hex[:REPEATER_ID_HEX_LENGTH]
    if _structured_or_preferred(public_key_hex, repeater_id):
        return True

    # --- words, repeats, extensions and compounds at the prefix -----------
    prefix_words = [
        word for word in _PREFIX_WORDS_BY_TRIPLE.get(public_key_hex[:3], ())
        if public_key_hex.startswith(word)
    ]
    for word in prefix_words:
        alternatives = words_of_length(len(word))
        repeated = repeated_match_length(public_key_hex, 0, word)
        if repeated >= len(word) * 2:
            if rarity_bits("word_repeat", repeated, alternatives, 1) >= required:
                return True
        extension = len(word)
        while extension < len(public_key_hex) and public_key_hex[extension] == word[-1]:
            extension += 1
        if extension > len(word):
            if rarity_bits("word_extension", extension, alternatives, 1) >= required:
                return True
    if prefix_words:
        chain_length, _chain = _word_chain(public_key_hex)
        if chain_length >= MIN_PERIODIC_LENGTH:
            if rarity_bits("compound_words", chain_length, chain_count(chain_length), 1) >= required:
                return True

    # --- runs (cheapest regex, and the most common qualifying shape) ------
    for match in _QUICK_RUN_REGEX.finditer(public_key_hex):
        length = len(match.group(0))
        if rarity_bits("single_run", length - 1, 1, _positions(length, match.start())) >= required:
            return True

    # --- plain catalog words ----------------------------------------------
    # Thresholds are precomputed per length, so a high cutoff discards most
    # lengths with a float comparison and never touches a regex.
    for length, prefix_bits, anywhere_bits in _WORD_THRESHOLDS:
        if prefix_bits >= required and public_key_hex[:length] in WORD_SETS_BY_LENGTH[length]:
            return True
        if anywhere_bits >= required and WORD_REGEX_BY_LENGTH[length].search(public_key_hex, 1) is not None:
            return True

    # --- periodic units ----------------------------------------------------
    # A unit of length u repeated to length M needs M - u constrained nibbles,
    # so the cutoff sets a floor on M before any scanning is worthwhile.
    if _min_periodic_length(required) <= PUBLIC_KEY_HEX_LENGTH and _QUICK_PERIODIC_REGEX.search(public_key_hex) is not None:
        for start in range(len(public_key_hex) - MIN_PERIODIC_LENGTH + 1):
            remaining = len(public_key_hex) - start
            for unit_length in range(2, min(MAX_PERIODIC_UNIT, remaining // 2) + 1):
                unit = public_key_hex[start:start + unit_length]
                repeated = repeated_match_length(public_key_hex, start, unit)
                if repeated < max(MIN_PERIODIC_LENGTH, unit_length * 2):
                    continue
                if rarity_bits("periodic", repeated - unit_length, 1, _positions(repeated, start)) >= required:
                    return True

    # --- palindromes -------------------------------------------------------
    if _min_palindrome_length(required) <= PUBLIC_KEY_HEX_LENGTH and _QUICK_PALINDROME_REGEX.search(public_key_hex) is not None:
        for start, length in _longest_palindromes(public_key_hex):
            positions = 1 if start == 0 else PALINDROME_CENTRES
            if rarity_bits("palindrome", length // 2, 1, positions) >= required:
                return True

    # --- sequences ---------------------------------------------------------
    if _QUICK_SEQUENCE_REGEX.search(public_key_hex) is not None:
        for start in range(len(public_key_hex) - MIN_SEQUENCE_LENGTH + 1):
            for step in (1, -1):
                length = _sequence_length(public_key_hex, start, step)
                if length < MIN_SEQUENCE_LENGTH:
                    continue
                if rarity_bits("sequence", length, _sequence_alternatives(length), _positions(length, start)) >= required:
                    return True

    return False
