"""Read-only deterministic style checks adapted from the v4.6.0 guardrail."""
from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    start: int
    end: int
    excerpt: str
    source_hash: str


RULES = (
    ("style.cliche-transition", "warning", "检测到模板化转折", re.compile(r"(?:与此同时|就在这时|不知过了多久)")),
    ("style.excessive-simile", "info", "同句中的明喻密度偏高", re.compile(r"[^。！？\n]*(?:仿佛|好像|宛如)[^。！？\n]*(?:仿佛|好像|宛如)[^。！？\n]*")),
    ("style.over-rationalized", "warning", "检测到过度解释式表达", re.compile(r"(?:这意味着|换句话说|显而易见的是)")),
)


def scan_language_style(content: str) -> tuple[Finding, ...]:
    source_hash = sha256(content.encode("utf-8")).hexdigest()
    findings: list[Finding] = []
    for rule_id, severity, message, pattern in RULES:
        for match in pattern.finditer(content):
            findings.append(Finding(rule_id, severity, message, match.start(), match.end(), match.group(0), source_hash))
    return tuple(sorted(findings, key=lambda finding: (finding.start, finding.rule_id)))
