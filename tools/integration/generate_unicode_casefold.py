"""Generate the frozen Unicode casefold contract used by Python and TypeScript.

The table is intentionally generated from Python's UCD 15.0.0 implementation,
not from the Unicode database installed on the consumer machine.  The checked
in JSON is the runtime artifact; this script is only the reproducible generator.
"""
from __future__ import annotations

import json
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path


EXPECTED_UCD = "15.0.0"
ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts" / "unicode-casefold-v1.json"
HANGUL = {
    "s_base": 0xAC00,
    "l_base": 0x1100,
    "v_base": 0x1161,
    "t_base": 0x11A7,
    "l_count": 19,
    "v_count": 21,
    "t_count": 28,
}


def build_table() -> dict[str, object]:
    if unicodedata.unidata_version != EXPECTED_UCD:
        raise RuntimeError(
            f"generator requires Python Unicode data {EXPECTED_UCD}; "
            f"runtime provides {unicodedata.unidata_version}"
        )
    mappings: dict[str, str] = {}
    decomposition: dict[str, list[int]] = {}
    combining_class: dict[str, int] = {}

    # Hangul syllables use the Unicode algorithmic decomposition rather than
    # entries in UnicodeData.txt.  Materialize the algorithm into the shared
    # contract so Python and TypeScript consume identical NFC data instead of
    # relying on their host runtime's Unicode version.
    n_count = HANGUL["v_count"] * HANGUL["t_count"]
    s_count = HANGUL["l_count"] * n_count
    for s_index in range(s_count):
        t_index = s_index % HANGUL["t_count"]
        l_part = HANGUL["l_base"] + s_index // n_count
        v_part = HANGUL["v_base"] + (s_index % n_count) // HANGUL["t_count"]
        parts = [l_part, v_part]
        if t_index:
            parts.append(HANGUL["t_base"] + t_index)
        decomposition[f"{HANGUL['s_base'] + s_index:04x}"] = parts

    for codepoint in range(0x110000):
        character = chr(codepoint)
        raw_decomposition = unicodedata.decomposition(character).split()
        if raw_decomposition and not raw_decomposition[0].startswith("<"):
            decomposition[f"{codepoint:04x}"] = [int(item, 16) for item in raw_decomposition]
        ccc = unicodedata.combining(character)
        if ccc:
            combining_class[f"{codepoint:04x}"] = ccc

    @lru_cache(maxsize=None)
    def canonical_decompose(codepoint: int) -> tuple[int, ...]:
        parts = decomposition.get(f"{codepoint:04x}")
        if parts is None:
            return (codepoint,)
        return tuple(item for part in parts for item in canonical_decompose(part))

    composition: dict[str, int] = {}
    # A final character can have a canonical decomposition longer than two
    # code points.  NFC composes its starter with each mark in sequence, so
    # collect those intermediate starter/mark pairs as well (for example
    # U+01D5: U+0055 + U+0308 + U+0304).
    for raw_codepoint in decomposition:
        codepoint = int(raw_codepoint, 16)
        parts = canonical_decompose(codepoint)
        starter = parts[0]
        last_ccc = 0
        for part in parts[1:]:
            ccc = unicodedata.combining(chr(part))
            absorbed = False
            if last_ccc == 0 or last_ccc < ccc:
                candidate = unicodedata.normalize("NFC", chr(starter) + chr(part))
                if len(candidate) == 1 and candidate != chr(starter):
                    composition[f"{starter:04x}+{part:04x}"] = ord(candidate)
                    starter = ord(candidate)
                    absorbed = True
            if absorbed:
                # The absorbed mark is not a blocking mark.  Keep the
                # previous unabsorbed CCC for the next composition attempt.
                continue
            else:
                if ccc == 0:
                    starter = part
                last_ccc = ccc
    for codepoint in range(0x110000):
        character = chr(codepoint)
        folded = character.casefold()
        if folded != character:
            mappings[f"{codepoint:04x}"] = folded
    return {
        "schema": "unicode-casefold/v1",
        "algorithm": "NFC followed by per-code-point full casefold mapping",
        "unicode_data_version": EXPECTED_UCD,
        "generator": "python-unicodedata",
        "mappings": mappings,
        "nfc_decomposition": decomposition,
        "nfc_combining_class": combining_class,
        "nfc_composition": composition,
        "nfc_hangul": HANGUL,
        "test_vectors": [
            {"input": chr(0xA7CB) + ".txt", "expected": chr(0xA7CB) + ".txt"},
            {"input": chr(0x0264) + ".txt", "expected": chr(0x0264) + ".txt"},
            {"input": "Straße.txt", "expected": "strasse.txt"},
            {"input": "cafe" + chr(0x0301) + ".txt", "expected": "café.txt"},
            # These vectors exercise consecutive canonical composition with
            # equal CCC values.  The second mark must still compose after the
            # first one is absorbed into the starter.
            {"input": "U" + chr(0x0308) + chr(0x0304), "expected": chr(0x01D5).casefold()},
            {"input": chr(0x03B9) + chr(0x0308) + chr(0x0301), "expected": chr(0x0390).casefold()},
            {"input": "\uAC00", "expected": "\uAC00"},
            {"input": "\u1100\u1161", "expected": "\uAC00"},
            {"input": "\uAC01", "expected": "\uAC01"},
            {"input": "\u1100\u1161\u11A8", "expected": "\uAC01"},
        ],
        "nfc_test_vectors": [
            {"input": "U" + chr(0x0308) + chr(0x0304), "expected": chr(0x01D5)},
            {"input": chr(0x03B9) + chr(0x0308) + chr(0x0301), "expected": chr(0x0390)},
            {"input": "\u1100\u1161", "expected": "\uAC00"},
            {"input": "\u1100\u1161\u11A8", "expected": "\uAC01"},
        ],
    }


def main() -> int:
    payload = build_table()
    rendered = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=False) + "\n").encode("utf-8")
    if "--check" in sys.argv[1:]:
        if not OUTPUT.is_file():
            print(f"missing generated contract: {OUTPUT}", file=sys.stderr)
            return 1
        if OUTPUT.read_bytes() != rendered:
            print(f"generated contract is stale: {OUTPUT}", file=sys.stderr)
            return 1
        print(f"checked {OUTPUT} ({len(payload['mappings'])} mappings)")
        return 0
    OUTPUT.write_bytes(rendered)
    print(f"wrote {OUTPUT} ({len(payload['mappings'])} mappings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
