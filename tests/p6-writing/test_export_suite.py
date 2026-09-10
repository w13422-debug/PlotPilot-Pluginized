from __future__ import annotations

import io
import json
import re
import time
import zipfile
import zlib
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest
from plotpilot_export_suite.domain import (
    ChapterRevision,
    ExportDocument,
    ExportFormat,
    ExportSelection,
    build_export,
    safe_filename_stem,
    validate_selection,
)


def revision(number: int, title: str, content: str) -> ChapterRevision:
    return ChapterRevision(f"doc-{number}", f"rev-{number}", number, title, content, sha256(content.encode()).hexdigest())


def document() -> ExportDocument:
    return ExportDocument("ws-1", "novel-1", ' 星 河：终章? ', "作者", "简介", (revision(2, "", "第二章"), revision(1, "开端", "第一章\r\n次行")))


def test_runtime_package_is_not_faked_before_real_ports_exist() -> None:
    root = Path(__file__).parents[2] / "first-party-plugins/export-suite"
    manifest = json.loads((root / "plugin.json").read_text("utf-8"))
    assert manifest["kind"] == "code"
    assert manifest["plugin_id"] == "com.plotpilot.export-suite"
    entrypoint = manifest["backend"]["entrypoint"]
    assert entrypoint == "plotpilot_export_suite.worker:main"
    module_name, separator, function_name = entrypoint.partition(":")
    assert separator == ":"
    entrypoint_path = root / "backend" / "src" / Path(*module_name.split(".")).with_suffix(".py")
    assert entrypoint_path.is_file()
    assert f"def {function_name}" in entrypoint_path.read_text("utf-8")
    assert (root / "backend/requirements.lock").is_file()
    assert not list(root.rglob("*.whl"))


def test_markdown_preserves_legacy_order_filename_and_utf8_without_bom() -> None:
    result = build_export(document(), ExportFormat.MARKDOWN)
    decoded = result.content.decode("utf-8")
    assert decoded.index("## 开端") < decoded.index("## 第 2 章")
    # Legacy Markdown preserves the chapter body's newline bytes.
    assert "第一章\r\n次行" in decoded
    assert not result.content.startswith(b"\xef\xbb\xbf")
    assert result.filename == "星_河：终章.md"
    assert result.media_type == "text/markdown; charset=utf-8"
    assert result.sha256 == sha256(result.content).hexdigest()
    assert result.source_revisions == (("doc-1", "rev-1", document().chapters[1].content_hash), ("doc-2", "rev-2", document().chapters[0].content_hash))


def test_single_chapter_uses_legacy_filename_and_exact_revision() -> None:
    result = build_export(document(), ExportFormat.MARKDOWN, document_id="doc-2")
    assert result.filename == "星_河：终章__-第2章.md"
    assert "第一章" not in result.content.decode("utf-8")
    assert result.source_revisions[0][1] == "rev-2"


def test_failure_cannot_change_frozen_body_or_source_revision() -> None:
    source = document()
    before = source.chapters
    with pytest.raises(ValueError, match="does not exist"):
        build_export(source, ExportFormat.MARKDOWN, document_id="missing")
    assert source.chapters == before
    with pytest.raises(FrozenInstanceError):
        source.chapters[0].content = "changed"  # type: ignore[misc]


def test_current_revision_hash_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="current revision hash"):
        ChapterRevision("doc", "rev", 1, "title", "body", "0" * 64)


@pytest.mark.parametrize("legacy,plugin", [(False, False), (True, True)])
def test_exporter_capability_flag_rejects_zero_or_two_owners(legacy: bool, plugin: bool) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        validate_selection(ExportSelection(legacy, plugin))


def test_filename_legacy_sanitization_and_limit() -> None:
    assert safe_filename_stem('  a b<c>:d/\\e|f?g*  ') == "a_b_c__d__e_f_g"
    assert len(safe_filename_stem("字" * 100)) == 80


@pytest.mark.parametrize(
    ("export_format", "suffix", "media_type", "magic"),
    [
        (ExportFormat.DOCX, ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK"),
        (ExportFormat.EPUB, ".epub", "application/epub+zip", b"PK"),
        (ExportFormat.PDF, ".pdf", "application/pdf", b"%PDF"),
    ],
)
def test_binary_legacy_formats_are_real_files(export_format: ExportFormat, suffix: str, media_type: str, magic: bytes) -> None:
    result = build_export(document(), export_format)
    assert result.filename == f"星_河：终章{suffix}"
    assert result.media_type == media_type
    assert result.content.startswith(magic)
    assert len(result.content) > 100
    if export_format is ExportFormat.PDF:
        assert b"/ToUnicode" in result.content


def _pdf_text(data: bytes) -> str:
    streams: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        stream = match.group(1)
        try:
            stream = zlib.decompress(stream)
        except zlib.error:
            pass
        streams.append(stream)
    cmap: dict[int, str] = {}
    for stream in streams:
        for cid, codepoint in re.findall(rb"<([0-9A-Fa-f]{4})> <([0-9A-Fa-f]{4,6})>", stream):
            cmap[int(cid, 16)] = chr(int(codepoint, 16))

    def unescape(value: bytes) -> bytes:
        return re.sub(rb"\\([nrtbf()\\])", lambda match: {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(match.group(1), match.group(1)), value)

    parts: list[str] = []
    for stream in streams:
        for literal in re.findall(rb"\((.*?)\) Tj", stream, re.DOTALL):
            raw = unescape(literal)
            if len(raw) % 2 == 0:
                parts.append("".join(cmap.get(int.from_bytes(raw[index:index + 2], "big"), "�") for index in range(0, len(raw), 2)))
    return "\n".join(parts)


def test_binary_formats_contain_chinese_in_current_revision_order() -> None:
    source = document()
    docx = build_export(source, ExportFormat.DOCX)
    with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
        docx_xml = archive.read("word/document.xml").decode("utf-8")
    assert docx_xml.index("开端") < docx_xml.index("第一章") < docx_xml.index("第 2 章") < docx_xml.index("第二章")

    epub = build_export(source, ExportFormat.EPUB)
    with zipfile.ZipFile(io.BytesIO(epub.content)) as archive:
        first = archive.read(next(name for name in archive.namelist() if name.endswith("chap_001.xhtml"))).decode("utf-8")
        second = archive.read(next(name for name in archive.namelist() if name.endswith("chap_002.xhtml"))).decode("utf-8")
    assert "开端" in first and "第一章" in first
    assert "第 2 章" in second and "第二章" in second

    pdf = build_export(source, ExportFormat.PDF)
    pdf_text = _pdf_text(pdf.content)
    assert pdf_text.index("开端") < pdf_text.index("第一章") < pdf_text.index("第 2 章") < pdf_text.index("第二章")
    assert "�" not in pdf_text


def test_single_chapter_selects_exact_document_not_duplicate_number() -> None:
    duplicate = revision(2, "重复号", "另一章")
    source = document()
    expanded = ExportDocument(source.workspace_id, source.novel_id, source.title, source.author, source.premise, source.chapters + (duplicate,))
    result = build_export(expanded, ExportFormat.MARKDOWN, document_id="doc-1")
    assert result.source_revisions == (("doc-1", "rev-1", source.chapters[1].content_hash),)


def test_repeated_render_is_byte_and_sha256_deterministic_for_every_format() -> None:
    source = document()
    first = {export_format: build_export(source, export_format) for export_format in ExportFormat}
    # Cross a PDF second and ZIP timestamp boundary; a wall-clock dependent
    # implementation would fail this assertion.
    time.sleep(2.0)
    second = {export_format: build_export(source, export_format) for export_format in ExportFormat}
    for export_format in ExportFormat:
        assert second[export_format].content == first[export_format].content
        assert second[export_format].sha256 == first[export_format].sha256
