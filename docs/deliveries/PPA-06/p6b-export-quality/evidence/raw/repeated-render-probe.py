from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "first-party-plugins/export-suite/backend/src"))

from plotpilot_export_suite import (  # noqa: E402
    ChapterRevision,
    ExportDocument,
    ExportFormat,
    build_export,
)


def _revision(number: int, title: str, content: str) -> ChapterRevision:
    return ChapterRevision(
        f"doc-{number}",
        f"rev-{number}",
        number,
        title,
        content,
        sha256(content.encode("utf-8")).hexdigest(),
    )


def main() -> int:
    document = ExportDocument(
        "ws-probe",
        "novel-probe",
        "星河：重复渲染",
        "探针作者",
        "固定输入",
        (_revision(2, "终章", "第二章正文"), _revision(1, "开端", "第一章正文\n次行")),
    )
    first = {fmt: build_export(document, fmt) for fmt in ExportFormat}
    time.sleep(2.0)
    second = {fmt: build_export(document, fmt) for fmt in ExportFormat}
    for fmt in ExportFormat:
        left, right = first[fmt], second[fmt]
        if left.content != right.content or left.sha256 != right.sha256:
            raise AssertionError(f"non-deterministic export: {fmt.value}")
        if left.content.startswith(b"\xef\xbb\xbf"):
            raise AssertionError(f"UTF-8 BOM in output: {fmt.value}")
        print(
            f"format={fmt.value} bytes={len(left.content)} sha256={left.sha256} "
            f"filename={left.filename} repeat=equal"
        )
    print("REPEATED_RENDER_PROBE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
