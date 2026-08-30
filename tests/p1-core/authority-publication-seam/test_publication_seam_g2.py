from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.publication.application import PublicationApplication
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)
from backend.plotpilot_core.repositories.authority_application.errors import (
    CrossWorkspaceError,
    IncompletePublicationError,
    OperationKeyReuseError,
    StaleCasError,
    UnknownReferenceError,
)


def _stack(tmp_path: Path):
    repo = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repo.create_workspace(Workspace("ws-1", "Novel"))
    repo.create_workspace(Workspace("ws-2", "Novel"))
    repo.create_document(Document("doc-1", "ws-1", "Chapter"))
    repo.create_document(Document("doc-2", "ws-2", "Chapter"))
    base1 = repo.publish_revision(document_id="doc-1", content="old", expected_revision_id=None, created_by="user")
    base2 = repo.publish_revision(document_id="doc-2", content="other", expected_revision_id=None, created_by="user")
    repo.ensure_authority_application_schema()
    return repo, assets, base1, base2


def _item(base, payload, *, item_id: str, workspace_id: str = "ws-1", document_id: str = "doc-1", parents=()):
    target = {"workspace_id": workspace_id, "entity_kind": "document", "entity_id": document_id}
    return {
        "schema": "candidate-item/v1",
        "item_id": item_id,
        "item_kind": "document",
        "target": target,
        "mutation": {
            "mode": "replace",
            "payload_schema": "core/document-text/v1",
            "payload_hash": payload.sha256,
        },
        "payload_asset_id": payload.asset_id,
        "base": {"revision_id": base.revision_id, "content_hash": base.content_hash},
        "write_set": [{**target, "revision_id": base.revision_id, "content_hash": base.content_hash}],
        "parent_candidate_ids": list(parents),
        "source_refs": [],
        "status": "complete",
    }


def _rewrite_candidate(repo, candidate_id: str, item: dict):
    raw, digest = CandidateService._canonical(item)
    repo._connection.execute(
        "UPDATE candidate SET item_id=?,item_hash=?,item_json=? WHERE candidate_id=?",
        (item["item_id"], digest, raw, candidate_id),
    )


def _counts(repo):
    return {
        table: repo._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "revision",
            "publication_receipt",
            "execution_core_event",
            "execution_publication_binding",
            "core_authority_operation",
        )
    }


def _revision_command(base, candidate_id: str, *, operation_key: str, revision_id: str, content: str = "new"):
    return {
        "schema": "core-document-revision-create-command/v1",
        "operation_key": operation_key,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
        "revision_id": revision_id,
        "base_revision_id": base.revision_id,
        "content": content,
        "created_by": "user",
        "source_candidate_id": candidate_id,
        "payload_schema": "core/document-text/v1",
    }


def test_two_node_parent_cycle_is_rejected_before_any_publication_write(tmp_path):
    repo, assets, base, _ = _stack(tmp_path)
    a_payload = assets.put(b"a", mime="text/plain", logical_role="candidate_payload", provenance="test")
    b_payload = assets.put(b"b", mime="text/plain", logical_role="candidate_payload", provenance="test")
    candidates = CandidateService(repo, assets)
    a = candidates.stage("a", _item(base, a_payload, item_id="a"))
    b = candidates.stage("b", _item(base, b_payload, item_id="b"))
    _rewrite_candidate(repo, a.candidate_id, _item(base, a_payload, item_id="a", parents=(b.candidate_id,)))
    _rewrite_candidate(repo, b.candidate_id, _item(base, b_payload, item_id="b", parents=(a.candidate_id,)))
    before = _counts(repo)
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "cycle-op",
        "workspace_id": "ws-1",
        "candidate_id": a.candidate_id,
        "accepted_by": "user",
    }
    with pytest.raises(IncompletePublicationError) as caught:
        app.accept(command)
    assert caught.value.error_code == "incomplete_publication"
    assert _counts(repo) == before
    assert repo.get_document("doc-1").content == "old"


def test_three_node_cycle_and_missing_grandparent_are_typed_and_atomic(tmp_path):
    repo, assets, base, _ = _stack(tmp_path)
    candidates = CandidateService(repo, assets)
    values = {}
    for name in ("a", "b", "c"):
        payload = assets.put(name.encode(), mime="text/plain", logical_role="candidate_payload", provenance="test")
        values[name] = (payload, candidates.stage(name, _item(base, payload, item_id=name)))
    _rewrite_candidate(repo, values["a"][1].candidate_id, _item(base, values["a"][0], item_id="a", parents=(values["b"][1].candidate_id,)))
    _rewrite_candidate(repo, values["b"][1].candidate_id, _item(base, values["b"][0], item_id="b", parents=(values["c"][1].candidate_id,)))
    _rewrite_candidate(repo, values["c"][1].candidate_id, _item(base, values["c"][0], item_id="c", parents=(values["a"][1].candidate_id,)))
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "three-cycle",
        "workspace_id": "ws-1",
        "candidate_id": values["a"][1].candidate_id,
        "accepted_by": "user",
    }
    with pytest.raises(IncompletePublicationError):
        app.accept(command)
    # Replace c's parent with a durable-looking but absent grandparent.
    missing = _item(base, values["c"][0], item_id="c", parents=("candidate-missing-grandparent",))
    _rewrite_candidate(repo, values["c"][1].candidate_id, missing)
    with pytest.raises(IncompletePublicationError):
        app.accept({**command, "publication_operation_key": "missing-grandparent"})
    assert _counts(repo)["publication_receipt"] == 0


def test_cross_workspace_parent_and_asset_metadata_or_bytes_drift_are_typed(tmp_path):
    repo, assets, base1, base2 = _stack(tmp_path)
    parent_payload = assets.put(b"parent", mime="text/plain", logical_role="candidate_payload", provenance="test")
    root_payload = assets.put(b"root", mime="text/plain", logical_role="candidate_payload", provenance="test")
    candidates = CandidateService(repo, assets)
    parent = candidates.stage("parent", _item(base2, parent_payload, item_id="parent", workspace_id="ws-2", document_id="doc-2"))
    root = candidates.stage("root", _item(base1, root_payload, item_id="root", parents=(parent.candidate_id,)))
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "cross-parent",
        "workspace_id": "ws-1",
        "candidate_id": root.candidate_id,
        "accepted_by": "user",
    }
    with pytest.raises(CrossWorkspaceError) as caught:
        app.accept(command)
    assert caught.value.error_code == "cross_workspace"
    assert _counts(repo)["publication_receipt"] == 0

    # Restore a same-workspace candidate, then corrupt metadata with an extra
    # field; AssetMetadata construction must be normalized to typed incomplete.
    same_parent = candidates.stage("same-parent", _item(base1, parent_payload, item_id="same-parent"))
    root2 = candidates.stage("root2", _item(base1, root_payload, item_id="root2", parents=(same_parent.candidate_id,)))
    metadata_path = assets.metadata / f"{root_payload.sha256}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["unexpected"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    command2 = {**command, "publication_operation_key": "metadata-extra", "candidate_id": root2.candidate_id}
    with pytest.raises(IncompletePublicationError) as caught:
        app.accept(command2)
    assert caught.value.error_code == "incomplete_publication"
    assert _counts(repo)["publication_receipt"] == 0


def test_publication_replay_rebinds_result_and_operation_key_is_typed(tmp_path):
    repo, assets, base, _ = _stack(tmp_path)
    payload = assets.put(b"new", mime="text/plain", logical_role="candidate_payload", provenance="test")
    staged = CandidateService(repo, assets).stage("stage", _item(base, payload, item_id="item"))
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "replay-op",
        "workspace_id": "ws-1",
        "candidate_id": staged.candidate_id,
        "accepted_by": "user",
    }
    first = app.accept(command)
    replay = app.accept(command)
    assert first["idempotent"] is False and replay["idempotent"] is True
    assert replay["publication_id"] == first["publication_id"]
    with pytest.raises(OperationKeyReuseError) as caught:
        app.accept({**command, "accepted_by": "other"})
    assert caught.value.error_code == "operation_key_reuse"
    # Durable result drift cannot be returned as a second authority.
    repo._connection.execute(
        "UPDATE core_authority_operation SET response_json=? WHERE route_id='publication.accept' AND operation_key='replay-op'",
        (json.dumps({**first, "entity_id": "forged"}),),
    )
    with pytest.raises(IncompletePublicationError):
        app.accept(command)


def test_unknown_root_candidate_is_a_typed_reference_failure(tmp_path):
    repo, assets, _base, _ = _stack(tmp_path)
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "unknown-root",
        "workspace_id": "ws-1",
        "candidate_id": "candidate-does-not-exist",
        "accepted_by": "user",
    }
    with pytest.raises(UnknownReferenceError) as caught:
        app.accept(command)
    assert caught.value.error_code == "unknown_reference"
    assert caught.value.status_code == 404
    assert caught.value.status == 404
    assert caught.value.to_error()["error_code"] == "unknown_reference"
    assert _counts(repo) == {
        "revision": 2,
        "publication_receipt": 0,
        "execution_core_event": 0,
        "execution_publication_binding": 0,
        "core_authority_operation": 0,
    }


def test_deep_staged_parent_closure_is_validated_and_publication_is_singleton(tmp_path):
    repo, assets, base, _ = _stack(tmp_path)
    candidates = CandidateService(repo, assets)
    payloads = {
        name: assets.put(name.encode(), mime="text/plain", logical_role="candidate_payload", provenance="test")
        for name in ("grandparent", "parent", "root")
    }
    grandparent = candidates.stage("grandparent", _item(base, payloads["grandparent"], item_id="grandparent"))
    parent = candidates.stage(
        "parent",
        _item(base, payloads["parent"], item_id="parent", parents=(grandparent.candidate_id,)),
    )
    root = candidates.stage(
        "root",
        _item(base, payloads["root"], item_id="root", parents=(parent.candidate_id,)),
    )
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "deep-closure",
        "workspace_id": "ws-1",
        "candidate_id": root.candidate_id,
        "accepted_by": "user",
    }
    result = app.accept(command)
    assert result["idempotent"] is False
    assert repo._connection.execute("SELECT status FROM candidate WHERE candidate_id=?", (root.candidate_id,)).fetchone()[0] == "published"
    assert repo._connection.execute("SELECT status FROM candidate WHERE candidate_id=?", (parent.candidate_id,)).fetchone()[0] == "staged"
    assert repo._connection.execute("SELECT status FROM candidate WHERE candidate_id=?", (grandparent.candidate_id,)).fetchone()[0] == "staged"
    assert repo._connection.execute("SELECT count(*) FROM publication_receipt").fetchone()[0] == 1
    assert repo._connection.execute("SELECT count(*) FROM core_authority_operation").fetchone()[0] == 1


def test_cross_workspace_grandparent_is_rejected_before_publication_write(tmp_path):
    repo, assets, base1, base2 = _stack(tmp_path)
    candidates = CandidateService(repo, assets)
    foreign_payload = assets.put(b"foreign", mime="text/plain", logical_role="candidate_payload", provenance="test")
    local_payload = assets.put(b"local", mime="text/plain", logical_role="candidate_payload", provenance="test")
    foreign = candidates.stage(
        "foreign", _item(base2, foreign_payload, item_id="foreign", workspace_id="ws-2", document_id="doc-2")
    )
    parent = candidates.stage("parent", _item(base1, local_payload, item_id="parent"))
    root = candidates.stage("root", _item(base1, local_payload, item_id="root", parents=(parent.candidate_id,)))
    _rewrite_candidate(
        repo,
        parent.candidate_id,
        _item(base1, local_payload, item_id="parent", parents=(foreign.candidate_id,)),
    )
    before = _counts(repo)
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "cross-grandparent",
        "workspace_id": "ws-1",
        "candidate_id": root.candidate_id,
        "accepted_by": "user",
    }
    with pytest.raises(CrossWorkspaceError) as caught:
        PublicationApplication(PublicationService(repo, assets)).accept(command)
    assert caught.value.error_code == "cross_workspace"
    assert _counts(repo) == before


def test_published_parent_requires_receipt_revision_and_event_lineage(tmp_path):
    repo, assets, base1, _ = _stack(tmp_path)
    repo.create_document(Document("doc-3", "ws-1", "Chapter"))
    base3 = repo.publish_revision(document_id="doc-3", content="base-3", expected_revision_id=None, created_by="user")
    parent_payload = assets.put(b"parent", mime="text/plain", logical_role="candidate_payload", provenance="test")
    root_payload = assets.put(b"root", mime="text/plain", logical_role="candidate_payload", provenance="test")
    candidates = CandidateService(repo, assets)
    parent = candidates.stage("published-parent", _item(base1, parent_payload, item_id="published-parent"))
    service = PublicationService(repo, assets)
    service.accept("published-parent-op", parent.candidate_id, created_by="user")
    root = candidates.stage(
        "published-parent-root",
        _item(base3, root_payload, item_id="published-parent-root", document_id="doc-3", parents=(parent.candidate_id,)),
    )
    # A valid published parent is accepted as closure evidence.
    service.accept("published-parent-root-op", root.candidate_id, created_by="user")
    assert repo.get_document("doc-3").content == "root"

    # Removing the parent's Event makes a subsequent closure replay fail
    # closed before any new Revision can be created.
    parent_receipt = repo._connection.execute(
        "SELECT operation_key FROM publication_receipt WHERE candidate_id=?", (parent.candidate_id,)
    ).fetchone()[0]
    repo._connection.execute("DELETE FROM execution_core_event WHERE event_id=?", (service._event_id(parent_receipt),))
    before = _counts(repo)
    with pytest.raises(IncompletePublicationError):
        service.preflight(root.candidate_id)
    assert _counts(repo) == before


@pytest.mark.parametrize("status", ["prepared", "partial", "failed", "skipped", "rejected", "deleted", "expired"])
def test_parent_lifecycle_and_partial_item_are_not_publication_visible(tmp_path, status):
    repo, assets, base, _ = _stack(tmp_path)
    candidates = CandidateService(repo, assets)
    parent_payload = assets.put(b"parent", mime="text/plain", logical_role="candidate_payload", provenance="test")
    root_payload = assets.put(b"root", mime="text/plain", logical_role="candidate_payload", provenance="test")
    parent = candidates.stage("parent", _item(base, parent_payload, item_id="parent"))
    root = candidates.stage("root", _item(base, root_payload, item_id="root", parents=(parent.candidate_id,)))
    if status == "partial":
        _rewrite_candidate(repo, parent.candidate_id, {**_item(base, parent_payload, item_id="parent"), "status": "partial"})
    else:
        repo._connection.execute("UPDATE candidate SET status=? WHERE candidate_id=?", (status, parent.candidate_id))
    before = _counts(repo)
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": f"parent-{status}",
        "workspace_id": "ws-1",
        "candidate_id": root.candidate_id,
        "accepted_by": "user",
    }
    with pytest.raises(IncompletePublicationError) as caught:
        PublicationApplication(PublicationService(repo, assets)).accept(command)
    assert caught.value.error_code == "incomplete_publication"
    assert _counts(repo) == before
    assert repo.get_document("doc-1").content == "old"


@pytest.mark.parametrize("tamper", ["missing", "wrong_shape", "wrong_type", "size", "hash", "bytes", "utf8"])
def test_asset_metadata_bytes_and_encoding_closure_fail_typed_and_atomic(tmp_path, tamper):
    repo, assets, base, _ = _stack(tmp_path)
    payload = assets.put(b"new", mime="text/plain", logical_role="candidate_payload", provenance="test")
    staged = CandidateService(repo, assets).stage("asset", _item(base, payload, item_id="item"))
    metadata_path = assets.metadata / f"{payload.sha256}.json"
    if tamper == "missing":
        metadata_path.unlink()
    elif tamper == "wrong_shape":
        metadata_path.write_text("[]", encoding="utf-8")
    elif tamper == "wrong_type":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["size"] = "3"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif tamper == "size":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["size"] = 99
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif tamper == "hash":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif tamper == "bytes":
        (assets.objects / payload.sha256[:2] / payload.sha256).write_bytes(b"tampered")
    else:
        (assets.objects / payload.sha256[:2] / payload.sha256).write_bytes(b"\xff\xfe\xfd")
    before = _counts(repo)
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": f"asset-{tamper}",
        "workspace_id": "ws-1",
        "candidate_id": staged.candidate_id,
        "accepted_by": "user",
    }
    with pytest.raises(IncompletePublicationError) as caught:
        PublicationApplication(PublicationService(repo, assets)).accept(command)
    assert caught.value.error_code == "incomplete_publication"
    assert _counts(repo) == before
    assert repo.get_document("doc-1").content == "old"


def test_aps_source_candidate_full_closure(tmp_path):
    repo, assets, base, _ = _stack(tmp_path)
    candidates = CandidateService(repo, assets)
    payloads = {
        name: assets.put(name.encode(), mime="text/plain", logical_role="candidate_payload", provenance="test")
        for name in ("grandparent", "parent", "new")
    }
    grandparent = candidates.stage(
        "source-grandparent",
        _item(base, payloads["grandparent"], item_id="source-grandparent"),
    )
    parent = candidates.stage(
        "source-parent",
        _item(base, payloads["parent"], item_id="source-parent", parents=(grandparent.candidate_id,)),
    )
    root = candidates.stage(
        "source-root",
        _item(base, payloads["new"], item_id="source-root", parents=(parent.candidate_id,)),
    )
    app = CoreAuthorityApplication(repo, PublicationService(repo, assets))

    result = app.execute_command(
        _revision_command(
            base,
            root.candidate_id,
            operation_key="source-full-closure",
            revision_id="revision-source-full-closure",
        )
    )

    assert result["source_candidate_id"] == root.candidate_id
    assert repo._connection.execute(
        "SELECT content FROM revision WHERE revision_id=?", (result["revision_id"],)
    ).fetchone()[0] == "new"
    assert repo._connection.execute(
        "SELECT count(*) FROM core_authority_operation WHERE route_id='document.revision.create'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "asset_deleted",
        "metadata_drift",
        "bytes_drift",
        "missing_ancestor",
        "cross_workspace_ancestor",
        "base_content_hash_drift",
    ],
)
def test_aps_zero_side_effects_on_source_drift(tmp_path, tamper):
    repo, assets, base1, base2 = _stack(tmp_path)
    candidates = CandidateService(repo, assets)
    parent_payload = assets.put(
        b"parent", mime="text/plain", logical_role="candidate_payload", provenance="test"
    )
    root_payload = assets.put(b"new", mime="text/plain", logical_role="candidate_payload", provenance="test")
    parent = candidates.stage("source-parent", _item(base1, parent_payload, item_id="source-parent"))
    root = candidates.stage(
        "source-root",
        _item(base1, root_payload, item_id="source-root", parents=(parent.candidate_id,)),
    )
    app = CoreAuthorityApplication(repo, PublicationService(repo, assets))

    if tamper == "asset_deleted":
        (assets.metadata / f"{root_payload.sha256}.json").unlink()
        (assets.objects / root_payload.sha256[:2] / root_payload.sha256).unlink()
    elif tamper == "metadata_drift":
        metadata_path = assets.metadata / f"{root_payload.sha256}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["size"] += 1
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif tamper == "bytes_drift":
        (assets.objects / root_payload.sha256[:2] / root_payload.sha256).write_bytes(b"tampered")
    elif tamper == "missing_ancestor":
        _rewrite_candidate(
            repo,
            parent.candidate_id,
            _item(base1, parent_payload, item_id="source-parent", parents=("candidate-missing",)),
        )
    elif tamper == "cross_workspace_ancestor":
        foreign_payload = assets.put(
            b"foreign", mime="text/plain", logical_role="candidate_payload", provenance="test"
        )
        foreign = candidates.stage(
            "source-foreign",
            _item(
                base2,
                foreign_payload,
                item_id="source-foreign",
                workspace_id="ws-2",
                document_id="doc-2",
            ),
        )
        _rewrite_candidate(
            repo,
            parent.candidate_id,
            _item(base1, parent_payload, item_id="source-parent", parents=(foreign.candidate_id,)),
        )
    else:
        with repo.transaction() as connection:
            connection.execute(
                "UPDATE revision SET content=? WHERE revision_id=?",
                ("drifted-base-content", base1.revision_id),
            )

    before = _counts(repo)
    current_revision = repo._connection.execute(
        "SELECT current_revision_id FROM document WHERE document_id='doc-1'"
    ).fetchone()[0]
    with pytest.raises(
        (IncompletePublicationError, UnknownReferenceError, CrossWorkspaceError, StaleCasError)
    ) as caught:
        app.execute_command(
            _revision_command(
                base1,
                root.candidate_id,
                operation_key=f"source-drift-{tamper}",
                revision_id=f"revision-source-drift-{tamper}",
            )
        )
    assert caught.value.error_code in {
        "incomplete_publication",
        "unknown_reference",
        "cross_workspace",
        "stale_cas",
    }
    assert _counts(repo) == before
    assert repo._connection.execute(
        "SELECT current_revision_id FROM document WHERE document_id='doc-1'"
    ).fetchone()[0] == current_revision


def test_aps_concurrent_identical_operation_replay(tmp_path):
    repo1, assets1, base, _ = _stack(tmp_path)
    payload = assets1.put(b"new", mime="text/plain", logical_role="candidate_payload", provenance="test")
    staged = CandidateService(repo1, assets1).stage("concurrent", _item(base, payload, item_id="concurrent"))
    repo2 = CoreAuthorityRepository(repo1.database)
    assets2 = AssetStore(assets1.root)
    services = (PublicationService(repo1, assets1), PublicationService(repo2, assets2))
    apps = tuple(PublicationApplication(service) for service in services)
    barrier = Barrier(2)

    for service in services:
        original = service.preflight

        def gated_preflight(candidate_id, *, expected_workspace_id=None, _original=original):
            preflight = _original(candidate_id, expected_workspace_id=expected_workspace_id)
            barrier.wait(timeout=10)
            return preflight

        service.preflight = gated_preflight

    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "concurrent-identical",
        "workspace_id": "ws-1",
        "candidate_id": staged.candidate_id,
        "accepted_by": "user",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda app: app.accept(command), apps))

    assert sorted(result["idempotent"] for result in results) == [False, True]
    first = next(result for result in results if result["idempotent"] is False)
    replay = next(result for result in results if result["idempotent"] is True)
    assert {**replay, "idempotent": False} == first
    assert repo1._connection.execute("SELECT count(*) FROM publication_receipt").fetchone()[0] == 1
    assert repo1._connection.execute("SELECT count(*) FROM execution_core_event").fetchone()[0] == 1
    assert repo1._connection.execute(
        "SELECT count(*) FROM core_authority_operation WHERE route_id='publication.accept'"
    ).fetchone()[0] == 1
    assert repo1._connection.execute(
        "SELECT count(*) FROM revision WHERE source_candidate_id=?", (staged.candidate_id,)
    ).fetchone()[0] == 1


def test_aps_operation_key_payload_conflict(tmp_path):
    repo, assets, base, _ = _stack(tmp_path)
    payload = assets.put(b"new", mime="text/plain", logical_role="candidate_payload", provenance="test")
    staged = CandidateService(repo, assets).stage("conflict", _item(base, payload, item_id="conflict"))
    app = PublicationApplication(PublicationService(repo, assets))
    command = {
        "schema": "publication-command/v1",
        "publication_operation_key": "payload-conflict",
        "workspace_id": "ws-1",
        "candidate_id": staged.candidate_id,
        "accepted_by": "user",
    }
    app.accept(command)
    before = _counts(repo)

    with pytest.raises(OperationKeyReuseError) as caught:
        app.accept({**command, "accepted_by": "different-user"})

    assert caught.value.error_code == "operation_key_reuse"
    assert _counts(repo) == before


def test_aps_delta_claim_consistency():
    root = Path(__file__).resolve().parents[3]
    delta = json.loads(
        (root / "coordination/PPA-01/authority-publication-seam/scoped-contract-delta-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert delta["status"] == "remediation_implemented_pending_same_reviewer_targeted_closure"
    assert delta["finding_disposition"] == {
        "NW-P1-APS-SOL-F-001": "CLOSED",
        "NW-P1-APS-SOL-F-002": "CLOSED",
        "NW-P1-APS-SOL-F-003": "CLOSED",
        "NW-P1-APS-SOL-F-004": "implemented_and_tested_pending_same_reviewer_targeted_closure",
        "NW-P1-APS-SOL-F-005": "implemented_and_tested_pending_same_reviewer_targeted_closure",
        "NW-P1-APS-SOL-F-006": "implemented_and_tested_pending_same_reviewer_targeted_closure",
    }
    assert delta["core_http_g2_unlocked"] is False
    assert delta["merge_eligible"] is False
    assert delta["closure_dependencies"] == [
        (
            "F-004 remains uncleared until the same reviewer accepts "
            "aps-source-candidate-full-closure, aps-zero-side-effects-on-source-drift, "
            "and aps-p1-regression evidence"
        ),
        (
            "F-006 remains uncleared until the same reviewer accepts "
            "aps-concurrent-identical-operation-replay, "
            "aps-operation-key-payload-conflict, and aps-publication-regression evidence"
        ),
        "Core HTTP G2 remains blocked until controller-serialized targeted closure",
    ]
    assert delta["stopped_or_deferred"] == [
        "publication.node_structure",
        "publication.relation_set",
        "publication.incomplete_stream",
        "cascade-dependent Workspace/Node delete semantics",
        "revision.content offset>total",
        "HTTP/app mount and public-contract/SDK changes",
        "automatic Publication and any second database/ledger",
    ]
    assert delta["scope_deviations"] == []
    assert delta["skips"] == []
    assert delta["central_acceptance"] == (
        "not performed; same-reviewer targeted closure remains mandatory"
    )
    assert delta["donor_push"] == "DISABLED"
