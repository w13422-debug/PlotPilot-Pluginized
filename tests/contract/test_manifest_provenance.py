from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_frozen_unicode_contract_is_content_addressed_and_licensed() -> None:
    contract_path = ROOT / "contracts" / "unicode-casefold-v1.json"
    manifest = json.loads((ROOT / "contracts" / "manifest-v1.json").read_text(encoding="utf-8"))
    records = [record for record in manifest["files"] if record.get("path") == "contracts/unicode-casefold-v1.json"]
    assert len(records) == 1
    record = records[0]
    raw = contract_path.read_bytes()
    assert record["bytes"] == len(raw)
    assert record["sha256"] == hashlib.sha256(raw).hexdigest()

    ledger = json.loads((ROOT / "docs" / "deliveries" / "PPA-00" / "license-ledger.json").read_text(encoding="utf-8"))
    unicode_data = ledger["unicode_data"]
    assert unicode_data == {
        "artifact": "contracts/unicode-casefold-v1.json",
        "generator": "tools/integration/generate_unicode_casefold.py",
        "license": "Unicode Data Files and Software License",
        "license_url": "https://www.unicode.org/license.txt",
        "name": "Unicode Data Files and Software License",
        "purpose": "Frozen cross-runtime NFC and full case-fold identity tables",
        "source": "CPython unicodedata backed by the Unicode Character Database",
        "unicode_data_version": "15.0.0",
    }

    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for marker in (
        "Unicode Data Files and Software License",
        "Unicode Character Database (UCD) **15.0.0**",
        "tools/integration/generate_unicode_casefold.py",
        "contracts/unicode-casefold-v1.json",
    ):
        assert marker in notice

    root_notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    for marker in (
        "Unicode Data Files and Software License",
        "Unicode Character Database (UCD) 15.0.0",
        "https://www.unicode.org/license.txt",
        "contracts/unicode-casefold-v1.json",
    ):
        assert marker in root_notice


def test_browser_dependency_pin_and_dependency_delta_are_content_addressed() -> None:
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"]["naive-ui"] == "2.44.1"

    lock_text = (ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    assert "specifier: 2.44.1" in lock_text
    assert "naive-ui@2.44.1:" in lock_text
    assert "naive-ui@2.45.2" not in lock_text

    delta_path = ROOT / "coordination" / "integration-queue" / "dependency-delta-naive-ui-2.44.1.json"
    delta = json.loads(delta_path.read_text(encoding="utf-8"))
    assert delta["schema"] == "dependency-delta/v1"
    assert delta["status"] == "accepted"
    assert delta["from_version"].startswith("2.45.2")
    assert delta["to_version"] == "2.44.1"
    assert delta["supply_chain_review"]["exact_version_pinned"] is True
