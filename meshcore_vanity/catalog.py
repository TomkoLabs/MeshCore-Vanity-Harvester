"""Hexspeak word catalog and curated preference lists.

The catalog is the *reference class* for word-shaped patterns: when the scorer
asks "how surprising is it that this key contains a catalog word", the honest
answer depends on how many words the catalog contains at that length. Those
counts are precomputed here so scoring.py can charge for them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

HEX_DIGITS = frozenset("0123456789ABCDEF")

HEXSPEAK_SUBSTITUTIONS: Mapping[str, str] = {
    "A": "A", "B": "B", "C": "C", "D": "D", "E": "E", "F": "F",
    "G": "6", "I": "1", "L": "1", "O": "0", "S": "5", "T": "7", "Z": "2",
}

# Curated exact prefixes. These carry *aesthetic* weight only. Their rarity is
# measured against the same open-ended reference class as everything else, so
# wanting a pattern can never make it mathematically rarer than it is.
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

# Quality is a sub-bit tie-breaker only; length-derived rarity stays dominant.
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

MINIMUM_WORD_LENGTH = 3


def _normalized_plain_word(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    ascii_word = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in ascii_word.upper() if c.isalpha())


def encode_hexspeak(word: str) -> Optional[Tuple[str, int]]:
    """Return (hex encoding, substitution count), or None if unencodable."""
    normalized = _normalized_plain_word(word)
    if not normalized:
        return None
    encoded: List[str] = []
    substitutions = 0
    for character in normalized:
        replacement = HEXSPEAK_SUBSTITUTIONS.get(character)
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
        if len(encoded) < MINIMUM_WORD_LENGTH or encoded.startswith(("00", "FF")):
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

WORDS_BY_FIRST: Mapping[str, Tuple[str, ...]] = {
    first: tuple(
        sorted(
            (word for word in HEX_WORDS if word.startswith(first)),
            key=lambda word: (-len(word), -HEX_WORDS[word][0], word),
        )
    )
    for first in HEX_DIGITS
}

WORD_LENGTHS_DESC: Tuple[int, ...] = tuple(sorted({len(word) for word in HEX_WORDS}, reverse=True))

WORD_SETS_BY_LENGTH: Mapping[int, frozenset] = {
    length: frozenset(word for word in HEX_WORDS if len(word) == length)
    for length in WORD_LENGTHS_DESC
}

WORD_REGEX_BY_LENGTH: Mapping[int, "re.Pattern[str]"] = {
    length: re.compile("|".join(sorted((re.escape(word) for word in words), key=len, reverse=True)))
    for length, words in WORD_SETS_BY_LENGTH.items()
}

# --- Reference-class sizes -------------------------------------------------
#
# A catalog hit is only as surprising as the catalog is small. Charging for
# these counts is what makes word patterns comparable with runs and periods.

WORD_COUNT_BY_LENGTH: Mapping[int, int] = {
    length: len(words) for length, words in WORD_SETS_BY_LENGTH.items()
}

MAX_CHAIN_LENGTH = 64


def _build_chain_counts() -> Tuple[int, ...]:
    """chains[n] = number of ordered catalog-word concatenations of length n.

    Used as the reference class for compound-word prefixes: finding *some*
    two-word compound is far less surprising than finding one specific pair.
    """
    counts = [0] * (MAX_CHAIN_LENGTH + 1)
    counts[0] = 1
    lengths = sorted(WORD_COUNT_BY_LENGTH)
    for total in range(1, MAX_CHAIN_LENGTH + 1):
        accumulated = 0
        for length in lengths:
            if length <= total:
                accumulated += WORD_COUNT_BY_LENGTH[length] * counts[total - length]
        counts[total] = accumulated
    return tuple(counts)


WORD_CHAIN_COUNTS: Tuple[int, ...] = _build_chain_counts()


def chain_count(total_length: int) -> int:
    if 0 <= total_length < len(WORD_CHAIN_COUNTS):
        return max(1, WORD_CHAIN_COUNTS[total_length])
    return 1


def words_of_length(length: int) -> int:
    return max(1, WORD_COUNT_BY_LENGTH.get(length, 1))


def is_hex(value: str) -> bool:
    return bool(value) and all(character in HEX_DIGITS for character in value)


def catalog_summary() -> str:
    return (
        f"{len(HEX_WORDS)} English/French encodings across "
        f"lengths {min(WORD_LENGTHS_DESC)}-{max(WORD_LENGTHS_DESC)}"
    )
