from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = str(ROOT / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.plugins.lifecycle.repository import (
    LIFECYCLE_MIGRATIONS,
    LifecycleRepository,
)
from backend.plotpilot_core.repositories.authority import (
    ChapterGenerationWriterFence,
    ConflictError,
    CoreAuthorityRepository,
    NotFoundError,
)
from backend.plotpilot_core.repositories.migrations import (
    CORE_MIGRATIONS,
    Migration,
    MigrationRunner,
)

FENCE_TABLE = "chapter_generation_writer_fence"
FENCE_MIGRATION = CORE_MIGRATIONS[-1]


@pytest.fixture
def fence_repository(tmp_path: Path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    repository.create_workspace(Workspace("ws-1", "Novel one"))
    repository.create_workspace(Workspace("ws-2", "Novel two"))
    repository.create_document(
        Document("chapter-1", "ws-1", "Chapter one", "core.chapter")
    )
    repository.create_document(
        Document("chapter-2", "ws-1", "Chapter two", "core.chapter")
    )
    repository.create_document(
        Document("chapter-other", "ws-2", "Other chapter", "core.chapter")
    )
    repository.create_document(
        Document("ordinary-1", "ws-1", "Ordinary document", "core.document")
    )
    try:
        yield repository
    finally:
        repository.close()


def _acquire(
    repository: CoreAuthorityRepository,
    *,
    writer_mode: str = "legacy",
    job_id: str = "job-legacy-1",
    operation_key: str = "acquire-legacy-1",
    workspace_id: str = "ws-1",
    chapter_document_id: str = "chapter-1",
    operation: str = "generate",
) -> ChapterGenerationWriterFence:
    return repository.acquire_chapter_generation_writer_fence(
        workspace_id=workspace_id,
        chapter_document_id=chapter_document_id,
        operation=operation,
        writer_mode=writer_mode,
        job_id=job_id,
        operation_key=operation_key,
    )


def _release(
    repository: CoreAuthorityRepository,
    fence: ChapterGenerationWriterFence,
    *,
    release_operation_key: str,
    workspace_id: str | None = None,
    chapter_document_id: str | None = None,
    operation: str | None = None,
    writer_mode: str | None = None,
    job_id: str | None = None,
    writer_epoch: int | None = None,
) -> ChapterGenerationWriterFence:
    return repository.release_chapter_generation_writer_fence(
        workspace_id=fence.workspace_id if workspace_id is None else workspace_id,
        chapter_document_id=(
            fence.chapter_document_id
            if chapter_document_id is None
            else chapter_document_id
        ),
        operation=fence.operation if operation is None else operation,
        writer_mode=fence.writer_mode if writer_mode is None else writer_mode,
        job_id=fence.job_id if job_id is None else job_id,
        writer_epoch=fence.writer_epoch if writer_epoch is None else writer_epoch,
        release_operation_key=release_operation_key,
    )


def _rows(repository: CoreAuthorityRepository) -> list[tuple[object, ...]]:
    with repository.read_connection() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {FENCE_TABLE} ORDER BY writer_epoch"
            ).fetchall()
        ]


def test_p1_fresh_production_repository_owns_lifecycle_schema(monkeypatch, tmp_path):
    def forbidden_initializer(cls, connection):
        del cls, connection
        raise AssertionError("production called the test-only lifecycle initializer")

    monkeypatch.setattr(
        LifecycleRepository,
        "initialize_standalone_schema_for_tests",
        classmethod(forbidden_initializer),
    )
    repository = CoreAuthorityRepository(tmp_path / "fresh.db")
    try:
        required = LifecycleRepository._REQUIRED_TABLES
        with repository.read_connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            receipt = connection.execute(
                "SELECT sha256 FROM schema_migration WHERE migration_id=?",
                (LIFECYCLE_MIGRATIONS[0].migration_id,),
            ).fetchone()
            lifecycle = LifecycleRepository(
                connection,
                transaction_factory=repository.transaction,
                core_authority_binding=repository,
            )

        assert required <= tables
        assert FENCE_TABLE in tables
        assert receipt[0] == LIFECYCLE_MIGRATIONS[0].sha256
        assert lifecycle.connection is repository._connection
        assert lifecycle.core_authority_binding is repository
        assert lifecycle.generation_state().current is None
    finally:
        repository.close()


def test_p2_existing_core_db_reopens_append_only_and_preserves_authority(tmp_path):
    database = tmp_path / "existing.db"
    connection = sqlite3.connect(database, isolation_level=None)
    old_migrations = CORE_MIGRATIONS[:-1]
    assert tuple(item.migration_id for item in old_migrations) == (
        "0001-core-authority",
        "0002-candidate-publication",
        "0003-chapter-authority",
    )
    assert FENCE_MIGRATION.migration_id == "0004-chapter-generation-writer-mode-fence"
    MigrationRunner(connection).apply(old_migrations)
    old_receipts = dict(
        connection.execute(
            "SELECT migration_id,sha256 FROM schema_migration"
        ).fetchall()
    )
    now = "2026-09-09T00:00:00Z"
    connection.execute(
        "INSERT INTO workspace VALUES(?,?,?,?,?,?,?,?,?)",
        ("ws-old", "WritingProject", "Existing", "active", None, "{}", now, now, 0),
    )
    connection.execute(
        "INSERT INTO document VALUES(?,?,?,?,?,?,?,?,?)",
        ("chapter-old", "ws-old", "core.chapter", "Old", "rev-old", "{}", now, now, 1),
    )
    content_hash = hashlib.sha256(b"preserved").hexdigest()
    connection.execute(
        "INSERT INTO revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rev-old",
            "ws-old",
            "chapter-old",
            None,
            None,
            "preserved",
            content_hash,
            "user",
            None,
            now,
            1,
            "core/document-text/v1",
        ),
    )
    connection.close()

    repository = CoreAuthorityRepository(database)
    try:
        with repository.read_connection() as reopened:
            receipts = dict(
                reopened.execute(
                    "SELECT migration_id,sha256 FROM schema_migration"
                ).fetchall()
            )
            lifecycle_tables = {
                row[0]
                for row in reopened.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'p2_plugin_%'"
                ).fetchall()
            }

        assert all(receipts[key] == value for key, value in old_receipts.items())
        assert receipts[FENCE_MIGRATION.migration_id] == FENCE_MIGRATION.sha256
        assert receipts[LIFECYCLE_MIGRATIONS[0].migration_id] == LIFECYCLE_MIGRATIONS[0].sha256
        assert LifecycleRepository._REQUIRED_TABLES <= lifecycle_tables
        assert repository.get_workspace("ws-old").title == "Existing"
        assert repository.get_revision("rev-old").content == "preserved"
    finally:
        repository.close()


def test_p3_legacy_acquire_same_key_replays_exactly_once(fence_repository):
    first_statements: list[str] = []
    fence_repository._connection.set_trace_callback(first_statements.append)
    first = _acquire(fence_repository)
    fence_repository._connection.set_trace_callback(None)

    reopened = CoreAuthorityRepository(fence_repository.database)
    replay_statements: list[str] = []
    try:
        reopened._connection.set_trace_callback(replay_statements.append)
        replay = _acquire(reopened)
        reopened._connection.set_trace_callback(None)

        assert first == replay
        assert first.writer_epoch == 1
        assert first.state == "active"
        assert len(_rows(reopened)) == 1
        assert sum(
            statement == "BEGIN IMMEDIATE"
            for statement in first_statements + replay_statements
        ) == 2
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("writer_mode", "job_id", "operation_key"),
    [
        ("plugin", "job-plugin-1", "acquire-plugin-1"),
        ("legacy", "job-legacy-2", "acquire-legacy-2"),
        ("plugin", "job-plugin-1", "acquire-legacy-1"),
    ],
)
def test_p4_active_writer_conflicts_without_mutation(
    fence_repository, writer_mode, job_id, operation_key
):
    original = _acquire(fence_repository)
    before = _rows(fence_repository)

    with pytest.raises(ConflictError):
        _acquire(
            fence_repository,
            writer_mode=writer_mode,
            job_id=job_id,
            operation_key=operation_key,
        )

    assert _rows(fence_repository) == before
    assert fence_repository.read_chapter_generation_writer_fence(
        workspace_id="ws-1",
        chapter_document_id="chapter-1",
        operation="generate",
    ) == original


def test_p5_exact_release_replays_then_other_mode_increments_once(fence_repository):
    first = _acquire(fence_repository)
    statements: list[str] = []
    fence_repository._connection.set_trace_callback(statements.append)
    released = _release(
        fence_repository, first, release_operation_key="release-legacy-1"
    )
    release_replay = _release(
        fence_repository, first, release_operation_key="release-legacy-1"
    )
    second = _acquire(
        fence_repository,
        writer_mode="plugin",
        job_id="job-plugin-1",
        operation_key="acquire-plugin-1",
    )
    acquire_replay = _acquire(
        fence_repository,
        writer_mode="plugin",
        job_id="job-plugin-1",
        operation_key="acquire-plugin-1",
    )
    fence_repository._connection.set_trace_callback(None)

    assert released == release_replay
    assert released.state == "released"
    assert released.released_at is not None
    assert second == acquire_replay
    assert second.writer_epoch == first.writer_epoch + 1 == 2
    assert second.state == "active"
    assert [row[6] for row in _rows(fence_repository)] == [1, 2]
    assert sum(statement == "BEGIN IMMEDIATE" for statement in statements) == 4


def test_p6_stale_or_wrong_release_never_clears_newer_active(fence_repository):
    first = _acquire(fence_repository)
    released = _release(
        fence_repository, first, release_operation_key="release-legacy-1"
    )
    active = _acquire(
        fence_repository,
        writer_mode="plugin",
        job_id="job-plugin-1",
        operation_key="acquire-plugin-1",
    )
    expected_rows = _rows(fence_repository)

    assert _release(
        fence_repository, first, release_operation_key="release-legacy-1"
    ) == released

    invalid_releases = [
        {"workspace_id": "ws-2", "release_operation_key": "release-wrong-ws"},
        {
            "chapter_document_id": "chapter-2",
            "release_operation_key": "release-wrong-chapter",
        },
        {"writer_mode": "legacy", "release_operation_key": "release-wrong-mode"},
        {"job_id": "job-plugin-2", "release_operation_key": "release-wrong-job"},
        {"writer_epoch": 99, "release_operation_key": "release-wrong-epoch"},
    ]
    for changes in invalid_releases:
        with pytest.raises((ConflictError, NotFoundError)):
            _release(fence_repository, active, **changes)
        assert _rows(fence_repository) == expected_rows

    with pytest.raises(ConflictError):
        _release(
            fence_repository,
            first,
            release_operation_key="release-wrong-replay-key",
        )
    assert _rows(fence_repository) == expected_rows
    assert fence_repository.read_chapter_generation_writer_fence(
        workspace_id="ws-1",
        chapter_document_id="chapter-1",
        operation="generate",
    ) == active


def test_p7_only_same_workspace_core_chapter_can_acquire(fence_repository):
    invalid = [
        {"chapter_document_id": "missing"},
        {"chapter_document_id": "chapter-other"},
        {"chapter_document_id": "ordinary-1"},
    ]
    for index, changes in enumerate(invalid):
        with pytest.raises((ConflictError, NotFoundError)):
            _acquire(
                fence_repository,
                operation_key=f"invalid-acquire-{index}",
                **changes,
            )

    assert _rows(fence_repository) == []


def test_p8_migration_failure_and_hash_drift_roll_back(tmp_path):
    connection = sqlite3.connect(tmp_path / "migration.db", isolation_level=None)
    runner = MigrationRunner(connection)
    runner.apply(CORE_MIGRATIONS[:-1])
    broken = Migration(
        FENCE_MIGRATION.migration_id,
        FENCE_MIGRATION.sql + "INVALID SQL;",
    )
    with pytest.raises(sqlite3.Error):
        runner.apply((broken,))

    assert connection.execute(
        "SELECT count(*) FROM schema_migration WHERE migration_id=?",
        (FENCE_MIGRATION.migration_id,),
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE name=?",
        (FENCE_TABLE,),
    ).fetchone()[0] == 0

    runner.apply((FENCE_MIGRATION,))
    with pytest.raises(RuntimeError, match="hash mismatch"):
        runner.apply(
            (
                Migration(
                    FENCE_MIGRATION.migration_id,
                    FENCE_MIGRATION.sql + "CREATE TABLE hash_drift(id INTEGER);",
                ),
            )
        )

    assert connection.execute(
        "SELECT sha256 FROM schema_migration WHERE migration_id=?",
        (FENCE_MIGRATION.migration_id,),
    ).fetchone()[0] == FENCE_MIGRATION.sha256
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='hash_drift'"
    ).fetchone()[0] == 0
    connection.close()


def test_p8_production_reopen_rejects_lifecycle_hash_drift(tmp_path):
    database = tmp_path / "lifecycle-drift.db"
    repository = CoreAuthorityRepository(database)
    repository.create_workspace(Workspace("ws-1", "Novel"))
    repository.create_document(
        Document("chapter-1", "ws-1", "Chapter", "core.chapter")
    )
    original = _acquire(repository)
    repository.close()

    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute(
        "UPDATE schema_migration SET sha256=? WHERE migration_id=?",
        ("0" * 64, LIFECYCLE_MIGRATIONS[0].migration_id),
    )
    connection.close()

    with pytest.raises(RuntimeError, match="migration hash mismatch"):
        CoreAuthorityRepository(database)

    verification = sqlite3.connect(database)
    verification.row_factory = sqlite3.Row
    try:
        stored = verification.execute(
            "SELECT * FROM chapter_generation_writer_fence"
        ).fetchone()
        receipt = verification.execute(
            "SELECT sha256 FROM schema_migration WHERE migration_id=?",
            (LIFECYCLE_MIGRATIONS[0].migration_id,),
        ).fetchone()
        assert CoreAuthorityRepository._writer_fence_from_row(stored) == original
        assert receipt[0] == "0" * 64
    finally:
        verification.close()
