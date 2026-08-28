from __future__ import annotations

import pytest

from support import authority_rows, complete_kwargs, make_candidate_bundle


@pytest.mark.parametrize(
    ("table", "operation"),
    [
        ("candidate", "INSERT"), ("execution_receipt", "INSERT"),
        ("execution_candidate_binding", "INSERT"), ("execution_job_event", "INSERT"),
        ("execution_core_event", "INSERT"), ("execution_attempt", "UPDATE"),
        ("execution_step", "UPDATE"), ("execution_job", "UPDATE"),
        ("execution_outcome", "INSERT"), ("p3_host_operation_ledger", "INSERT"),
    ],
)
def test_failure_at_each_terminal_write_rolls_back_every_db_effect(execution_stack, table, operation):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    repository = execution_stack["repository"]
    before = authority_rows(repository)
    repository._connection.execute(
        f"CREATE TEMP TRIGGER fail_{table} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'injected {table} failure'); END"
    )
    with pytest.raises(Exception, match="injected"):
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    assert authority_rows(repository) == before
    attempt = repository._connection.execute("SELECT state FROM execution_attempt").fetchone()[0]
    assert attempt == "running"


def test_publication_receipt_failure_rolls_back_revision_and_candidate_status(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    repository = execution_stack["repository"]
    candidate_id = repository._connection.execute("SELECT candidate_id FROM candidate").fetchone()[0]
    repository._connection.execute(
        "CREATE TEMP TRIGGER fail_publication BEFORE INSERT ON publication_receipt BEGIN SELECT RAISE(ABORT,'injected publication failure'); END"
    )
    from backend.plotpilot_core.publication import PublicationService
    with pytest.raises(Exception, match="injected"):
        PublicationService(repository, execution_stack["assets"]).accept("publish-1", candidate_id, created_by="user")
    assert repository.get_document("doc-1").content == "old"
    assert repository._connection.execute("SELECT status FROM candidate").fetchone()[0] == "staged"
    assert repository._connection.execute("SELECT count(*) FROM publication_receipt").fetchone()[0] == 0


def test_publication_event_failure_rolls_back_revision_receipt_and_status(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    repository = execution_stack["repository"]
    candidate_id = repository._connection.execute("SELECT candidate_id FROM candidate").fetchone()[0]
    repository._connection.execute(
        "CREATE TEMP TRIGGER fail_publication_event BEFORE INSERT ON execution_core_event BEGIN SELECT RAISE(ABORT,'injected publication event failure'); END"
    )
    from backend.plotpilot_core.publication import PublicationService
    with pytest.raises(Exception, match="injected"):
        PublicationService(repository, execution_stack["assets"]).accept("publish-1", candidate_id, created_by="user")
    assert repository.get_document("doc-1").content == "old"
    assert repository._connection.execute("SELECT status FROM candidate").fetchone()[0] == "staged"
    assert repository._connection.execute("SELECT count(*) FROM publication_receipt").fetchone()[0] == 0


def test_publication_binding_failure_rolls_back_revision_receipt_event_and_status(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    repository = execution_stack["repository"]
    candidate_id = repository._connection.execute("SELECT candidate_id FROM candidate").fetchone()[0]
    before_events = repository._connection.execute("SELECT count(*) FROM execution_core_event").fetchone()[0]
    repository._connection.execute(
        "CREATE TEMP TRIGGER fail_publication_binding BEFORE INSERT ON execution_publication_binding BEGIN SELECT RAISE(ABORT,'injected publication binding failure'); END"
    )
    from backend.plotpilot_core.publication import PublicationService
    with pytest.raises(Exception, match="injected"):
        PublicationService(repository, execution_stack["assets"]).accept("publish-1", candidate_id, created_by="user")
    assert repository.get_document("doc-1").content == "old"
    assert repository._connection.execute("SELECT status FROM candidate").fetchone()[0] == "staged"
    assert repository._connection.execute("SELECT count(*) FROM publication_receipt").fetchone()[0] == 0
    assert repository._connection.execute("SELECT count(*) FROM execution_publication_binding").fetchone()[0] == 0
    assert repository._connection.execute("SELECT count(*) FROM execution_core_event").fetchone()[0] == before_events


def test_publication_replay_fails_closed_when_its_core_event_is_missing(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    repository = execution_stack["repository"]
    candidate_id = repository._connection.execute("SELECT candidate_id FROM candidate").fetchone()[0]
    from backend.plotpilot_core.publication import PublicationService
    service = PublicationService(repository, execution_stack["assets"])
    service.accept("publish-1", candidate_id, created_by="user")
    repository._connection.execute(
        "DELETE FROM execution_core_event WHERE json_extract(event_json,'$.event_type')='revision.published'"
    )
    from backend.plotpilot_core.repositories import ConflictError
    with pytest.raises(ConflictError, match="incomplete"):
        service.accept("publish-1", candidate_id, created_by="user")
