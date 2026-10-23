"""Collision-safe, filesystem-portable file names for database objects.

Each name component (schema, table, object name) is encoded so that:

* ASCII letters, digits, space, ``_`` and ``-`` and non-ASCII alphanumerics are kept verbatim;
* every other character (including ``.``, which separates components) is
  percent-encoded as UTF-8 bytes, so ``a.b`` + ``c`` never collides with ``a`` + ``b.c``;
* Windows reserved device names are escaped.

Function signatures use ``name(type1, type2)``; inside the parentheses spaces,
dots and ``[]`` are kept for readability and parentheses/commas inside a type
name are encoded, so the literal ``(`` ``,`` ``)`` delimiters stay unambiguous.

Names that differ only by letter case would collide on case-insensitive file
systems; ``disambiguate_case`` appends a short deterministic hash to those.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

_ASCII_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
MAX_FILENAME = 180


def _encode(text: str, extra_safe: str = "") -> str:
    out: list[str] = []
    for ch in text:
        if ch in _ASCII_SAFE or ch in extra_safe or (ord(ch) > 127 and ch.isalnum()):
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def encode_component(name: str) -> str:
    if name == "":
        return "%00"
    encoded = _encode(name)
    if encoded.lower() in _WINDOWS_RESERVED:
        encoded = f"%{ord(encoded[0]):02X}{encoded[1:]}"
    # Leading/trailing spaces are easy to lose in tooling; keep them visible.
    if encoded.startswith(" "):
        encoded = "%20" + encoded[1:]
    if encoded.endswith(" "):
        encoded = encoded[:-1] + "%20"
    return encoded


def encode_type_name(type_name: str) -> str:
    return _encode(type_name, extra_safe=" .[]")


def object_filename(*components: str, signature: list[str] | None = None) -> str:
    """Build ``comp1.comp2.comp3[(sig)].sql`` with each component encoded."""
    stem = ".".join(encode_component(c) for c in components)
    if signature is not None:
        stem += "(" + ", ".join(encode_type_name(t) for t in signature) + ")"
    filename = stem + ".sql"
    if len(filename) > MAX_FILENAME:
        digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:10]
        filename = stem[: MAX_FILENAME - 16] + "~" + digest + ".sql"
    return filename


def disambiguate_case(paths: dict[str, str]) -> dict[str, str]:
    """Map identity-key -> relative path, appending ``~hash`` where paths differ only by case.

    ``paths`` maps a stable identity string to its proposed relative path.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for identity, path in paths.items():
        groups[path.lower()].append(identity)
    result = dict(paths)
    for identities in groups.values():
        if len(identities) < 2:
            continue
        for identity in identities:
            path = paths[identity]
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            stem, _, ext = path.rpartition(".")
            result[identity] = f"{stem}~{digest}.{ext}"
    return result
