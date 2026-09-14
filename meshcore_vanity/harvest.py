"""Compile a conservative native screening policy from the Python scorer.

Native backends screen; Python remains the authority for ranking and verifying
returned key material. Threshold 65 means unreachable in a 64-nibble key.
The flat array ABI is shared with mc-keygen/cuda/harvest_filter.h.
"""

from .catalog import HEX_WORDS, WORD_LENGTHS_DESC, chain_count, words_of_length
from .scoring import (
    MAX_PERIODIC_UNIT, MIN_PALINDROME_LENGTH, MIN_PERIODIC_LENGTH,
    MIN_RUN_LENGTH, MIN_SEQUENCE_LENGTH,
    _ALL_PREFERENCES, _PREFERENCES_BY_LENGTH, _sequence_alternatives,
    rarity_bits, required_rarity_bits,
)

POLICY_VERSION = 2
PERIOD_PREFIX = 8
PERIOD_ANYWHERE = 21
WORD_PREFIX = 34
WORD_ANYWHERE = 99
WORD_EXTENSION = 164
WORD_REPEAT = 229
COMPOUND = 294
COMPOUND_EXTENSION = 359
THRESHOLD_COUNT = 424


def build_policy(cutoff_score: int) -> dict:
    required = required_rarity_bits(cutoff_score)
    thresholds = [65] * THRESHOLD_COUNT
    thresholds[0] = POLICY_VERSION

    def first(minimum, measure, maximum=64):
        return next((length for length in range(minimum, maximum + 1)
                     if measure(length) >= required), 65)

    # v2 is prefix-only. Former anywhere slots stay reserved to keep the
    # table layout simple; native code never reads them.
    thresholds[1] = first(MIN_RUN_LENGTH, lambda n: rarity_bits("single_run", n - 1))
    thresholds[3] = first(MIN_SEQUENCE_LENGTH, lambda n:
        rarity_bits("sequence", n, _sequence_alternatives(n)), 16)
    thresholds[5] = first(MIN_PALINDROME_LENGTH, lambda n: rarity_bits("palindrome", n // 2))
    for unit in range(2, MAX_PERIODIC_UNIT + 1):
        thresholds[PERIOD_PREFIX + unit] = first(max(MIN_PERIODIC_LENGTH, 2 * unit), lambda n:
            rarity_bits("periodic", n - unit))

    # Small repeater-ID shapes are only relevant at low test/custom cutoffs.
    thresholds[7] = int(max(rarity_bits("structured_id", 4),
                            rarity_bits("structured_id", 3)) >= required)
    for length in range(65):
        for base in (WORD_PREFIX, WORD_ANYWHERE, COMPOUND):
            thresholds[base + length] = 0
    for length in WORD_LENGTHS_DESC:
        alternatives = words_of_length(length)
        thresholds[WORD_PREFIX + length] = int(rarity_bits("word", length, alternatives, 1) >= required)
        thresholds[WORD_EXTENSION + length] = first(length + 1, lambda n:
            rarity_bits("word_extension", n, alternatives, 1))
        thresholds[WORD_REPEAT + length] = first(2 * length, lambda n:
            rarity_bits("word_repeat", n, alternatives, 1))
    for length in range(MIN_PERIODIC_LENGTH, 65):
        thresholds[COMPOUND + length] = int(rarity_bits("compound_words", length, chain_count(length), 1) >= required)
        thresholds[COMPOUND_EXTENSION + length] = first(length + 1, lambda n:
            rarity_bits("compound_extension", n, chain_count(length) * 58, 1))

    # Trie nodes: 16 child indices (0 = missing), then terminal flags.
    # Flags: word=1, score-qualified preference=2, visible preference=4.
    nodes = [[0] * 17]

    def add(text, flag):
        node = 0
        for character in text:
            digit = int(character, 16)
            if not nodes[node][digit]:
                nodes[node][digit] = len(nodes)
                nodes.append([0] * 17)
            node = nodes[node][digit]
        nodes[node][16] |= flag

    for word in HEX_WORDS:
        add(word, 1)
    for prefix in _ALL_PREFERENCES:
        add(prefix, 4)  # visible-ID eligibility, independent of the score floor
        if rarity_bits("preference", len(prefix), _PREFERENCES_BY_LENGTH[len(prefix)], 1) >= required:
            add(prefix, 2)
    return {
        "version": POLICY_VERSION,
        "cutoff_score": int(cutoff_score),
        "thresholds": thresholds,
        "trie": [value for node in nodes for value in node],
    }
