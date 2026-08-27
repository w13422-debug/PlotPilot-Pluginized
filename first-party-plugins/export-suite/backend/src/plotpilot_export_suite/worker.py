"""Runtime entrypoint shell.

The actual P2/P3 framed host session is intentionally not duplicated here.
P2 loads this package and invokes :func:`build_export_request` from its real
worker runtime; the function returns artifact bytes plus provenance for the
real ``host.asset.create/v1`` call.
"""
from __future__ import annotations

from .domain import ChapterRevision, ExportDocument, ExportFormat, build_export


def build_export_request(params: dict) -> dict:
    document = ExportDocument(
        workspace_id=params["workspace_id"],
        novel_id=params["novel_id"],
        title=params.get("title", ""),
        author=params.get("author", ""),
        premise=params.get("premise", ""),
        chapters=tuple(ChapterRevision(**chapter) for chapter in params["current_revisions"]),
    )
    payload = build_export(
        document,
        ExportFormat(params["format"]),
        chapter_number=params.get("chapter_number"),
    )
    return {
        "content": payload.content,
        "media_type": payload.media_type,
        "filename": payload.filename,
        "sha256": payload.sha256,
        "source_revisions": payload.source_revisions,
    }

