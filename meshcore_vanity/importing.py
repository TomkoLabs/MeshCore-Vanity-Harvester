"""Discover private material independently of a legacy file's board layout.

Only labelled 32-byte values are seeds. Unlabelled 64-byte values may be
expanded MeshCore keys or self-consistent seed||public encodings; ordinary
32-byte public keys and checksums must never become imported seeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from .keys import loose_hex, meshcore_private_key_from_seed, public_key_from_seed

_PRIVATE_NAMES = frozenset((
    "privatekey", "privatekeyhex", "private", "secretkey", "secretkeyhex",
    "secret", "prvkey", "prvkeyhex", "prv", "sk", "expandedprivatekey",
))
_SEED_NAMES = frozenset(("seed", "seedhex", "ed25519seed", "privateseed", "secretseed"))
_PUBLIC_NAMES = frozenset(("publickey", "publickeyhex", "public", "pubkey", "pubkeyhex", "pub"))
_SOURCES = frozenset(("generic_cpu", "mc_keygen_cpu", "mc_keygen_gpu", "restored", "imported_private"))
_SET_COMMAND = re.compile(r"set\s+prv\.key\s+(\S+)\s*", re.IGNORECASE)


def field_name(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def private_text(value: str) -> Optional[str]:
    """An entire hex key or MeshCore command, never a substring in prose."""
    command = _SET_COMMAND.fullmatch(value.strip())
    normalized = loose_hex(command.group(1) if command else value)
    return normalized if normalized and len(normalized) == 128 else None


def _hex(value: Any, size: int) -> Optional[str]:
    # JSON booleans are not bytes, even though Python bool subclasses int.
    if isinstance(value, list) and any(type(item) is not int for item in value):
        return None
    normalized = loose_hex(value)
    return normalized if normalized and len(normalized) == size else None


def _metadata(value: Mapping[str, Any], inherited: Mapping[str, Any]) -> Dict[str, Any]:
    """Retain safe provenance, never arbitrary imported strings in public output."""
    result = dict(inherited)
    if isinstance(value.get("source"), str) and value["source"] in _SOURCES:
        result["source"] = value["source"]
    version = value.get("score_version")
    if type(version) is int and version >= 0:
        result["score_version"] = version
    attempts = value.get("found_after_attempts")
    if type(attempts) is int and attempts >= 0:
        result["found_after_attempts"] = attempts
    found = value.get("found_at")
    if isinstance(found, str):
        try:
            result["found_at"] = datetime.fromisoformat(found.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return result


@dataclass(frozen=True)
class PrivateCandidate:
    material: Optional[str]
    seed_value: bool
    public: Optional[str]
    invalid_public: bool
    seed_hint: Optional[str]
    metadata: Mapping[str, Any] = field(compare=False, hash=False, repr=False)


def collect_private_candidates(payload: Any) -> List[PrivateCandidate]:
    """Iteratively walk arbitrary JSON containers; retain pair associations.

    There is no board-name or fixed-depth assumption. A public-key map's key
    supplies the missing public field. A nested object's explicit public field
    takes precedence over an inherited outer field.
    """
    candidates: Dict[PrivateCandidate, PrivateCandidate] = {}
    stack = [(payload, "", None, False, None, {})]
    while stack:
        value, name, public, invalid_public, seed_hint, metadata = stack.pop()
        if isinstance(value, Mapping):
            if value.get("includes_secret_material") is False:
                continue
            publics = [item for key, item in value.items()
                       if field_name(key) in _PUBLIC_NAMES and item is not None]
            if publics:
                normalized = {_hex(item, 64) for item in publics}
                invalid_public = None in normalized or len(normalized) != 1
                public = next(iter(normalized)) if not invalid_public else None
            seeds = [_hex(item, 64) for key, item in value.items() if field_name(key) in _SEED_NAMES]
            seed_hint = next((item for item in seeds if item), seed_hint)
            metadata = _metadata(value, metadata)
            # A seed alongside private material is a verification hint, not a
            # second route that could hide a corrupted private/public pair.
            has_private = any(field_name(key) in _PRIVATE_NAMES or
                              (isinstance(item, str) and private_text(item) is not None) or
                              (isinstance(item, list) and _hex(item, 128) is not None)
                              for key, item in value.items() if item is not None)
            for key, item in reversed(list(value.items())):
                label = field_name(key)
                if label in ("value", "hex", "bytes", "data") and name in _PRIVATE_NAMES | _SEED_NAMES:
                    label = name
                if label in _PUBLIC_NAMES or label == "integritysha256":
                    continue
                if label in _SEED_NAMES and has_private:
                    continue
                map_public = _hex(key, 64)
                stack.append((item, label, map_public or public,
                              False if map_public else invalid_public, seed_hint, metadata))
            continue

        labelled = name in _PRIVATE_NAMES or name in _SEED_NAMES
        seed_value = name in _SEED_NAMES
        material = _hex(value, 64 if seed_value else 128)
        if not seed_value and material is None and isinstance(value, str):
            material = private_text(value)
        if labelled and material is None:
            material = _hex(value, 64)
        if isinstance(value, list) and material is None:
            for item in reversed(value):
                stack.append((item, name, public, invalid_public, seed_hint, metadata))
            continue
        if material is None and not labelled:
            continue
        if not labelled and (material is None or len(material) != 128):
            continue
        candidate = PrivateCandidate(material, seed_value, public, invalid_public, seed_hint, metadata)
        candidates.setdefault(candidate, candidate)
    return list(candidates.values())


def prepare_private_candidate(candidate: PrivateCandidate) -> Dict[str, Any]:
    """Normalize formats without expensive seedless scalar multiplication.

    Missing public keys are derived by the caller with progress reporting.
    Supplied public keys are always verified, never silently replaced.
    """
    if candidate.invalid_public or candidate.material is None:
        raise ValueError("invalid private material or public-key field")
    material = candidate.material
    public = candidate.public
    seed = None
    if len(material) == 64:
        seed = material
        private = meshcore_private_key_from_seed(bytes.fromhex(seed)).hex().upper()
        derived = public_key_from_seed(bytes.fromhex(seed)).hex().upper()
    elif len(material) == 128 and not candidate.seed_value:
        raw = bytes.fromhex(material)
        # Recognize libsodium's encoding by its cryptographic relationship,
        # rather than by length, which is shared with MeshCore expanded keys.
        seed_public = public_key_from_seed(raw[:32]).hex().upper()
        if raw[32:].hex().upper() == seed_public:
            seed = raw[:32].hex().upper()
            private = meshcore_private_key_from_seed(raw[:32]).hex().upper()
            derived = seed_public
        else:
            if raw[0] & 7 or raw[31] & 128 or not raw[31] & 64:
                raise ValueError("expanded private scalar is not clamped")
            private = material
            derived = None
            if candidate.seed_hint:
                hint = bytes.fromhex(candidate.seed_hint)
                if meshcore_private_key_from_seed(hint).hex().upper() == private:
                    seed = candidate.seed_hint
                    derived = public_key_from_seed(hint).hex().upper()
    else:
        raise ValueError("private material must be a 32-byte seed or 64-byte private key")
    if derived:
        if public and public != derived:
            raise ValueError("private material does not derive the supplied public key")
        public = derived
    return {**candidate.metadata, "source": candidate.metadata.get("source", "imported_private"),
            "public_key": public, "private_key": private, "seed": seed}
