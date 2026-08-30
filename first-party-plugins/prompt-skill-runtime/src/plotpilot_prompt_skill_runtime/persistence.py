"""Durable Prompt/Skill Runtime records on the composed Core SQLite owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plotpilot_plugin_sdk import ContractError, sha256_hex

from .attribution import (
    AttributionProof,
    FrozenModelInvocation,
    verify_model_receipt_asset,
)
from .chain import AssetRef, ChainExecution, SkillStep, verify_chain
from .immutability import thaw_json
from .package import SkillPackage

_MIGRATION_ID = "p2-prompt-skill-runtime-001"
_OWNED_PREFIX = "p2_skill_"


def _invalid(message: str, *, path: str | None = None) -> ContractError:
    return ContractError(1011, message, path=path)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load(value: str) -> Any:
    return json.loads(value)


def _migration_material() -> tuple[str, str]:
    root = Path(__file__).with_name("migrations")
    sql_bytes = (root / "001_prompt_skill_runtime.sql").read_bytes()
    if b"\r" in sql_bytes or not sql_bytes.endswith(b"\n"):
        raise RuntimeError("Prompt Skill migration must be LF-stable and newline terminated")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected_keys = {"schema", "migration_id", "path", "sha256"}
    if set(manifest) != expected_keys or manifest["schema"] != "prompt-skill-runtime-migration-manifest/v1":
        raise RuntimeError("Prompt Skill migration manifest shape is invalid")
    if manifest["migration_id"] != _MIGRATION_ID or manifest["path"] != "001_prompt_skill_runtime.sql":
        raise RuntimeError("Prompt Skill migration manifest identity drift")
    digest = hashlib.sha256(sql_bytes).hexdigest()
    if manifest["sha256"] != digest:
        raise RuntimeError("Prompt Skill migration source hash mismatch")
    return sql_bytes.decode("utf-8", errors="strict"), digest


def _complete_statements(sql: str) -> tuple[str, ...]:
    """Split only at boundaries recognized by SQLite itself."""

    pending = ""
    statements: list[str] = []
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                statements.append(pending)
            pending = ""
    if pending.strip():
        raise RuntimeError("Prompt Skill migration ends with incomplete SQLite input")
    return tuple(statements)


def _execute_migration(connection: sqlite3.Connection, sql: str) -> None:
    for statement in _complete_statements(sql):
        connection.execute(statement)


def _schema_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    objects = [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE ? OR tbl_name LIKE ? ORDER BY type,name",
            (f"{_OWNED_PREFIX}%", f"{_OWNED_PREFIX}%"),
        )
    ]
    table_names = sorted(
        str(row[1]) for row in objects if row[0] == "table" and str(row[1]).startswith(_OWNED_PREFIX)
    )
    tables: dict[str, Any] = {}
    for table in table_names:
        quoted = '"' + table.replace('"', '""') + '"'
        table_info = [tuple(row) for row in connection.execute(f"PRAGMA table_xinfo({quoted})")]
        foreign_keys = [tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({quoted})")]
        indexes = [tuple(row) for row in connection.execute(f"PRAGMA index_list({quoted})")]
        index_xinfo: dict[str, list[tuple[Any, ...]]] = {}
        for index in indexes:
            name = str(index[1])
            quoted_index = '"' + name.replace('"', '""') + '"'
            index_xinfo[name] = [tuple(row) for row in connection.execute(f"PRAGMA index_xinfo({quoted_index})")]
        tables[table] = {
            "table_xinfo": table_info,
            "foreign_key_list": foreign_keys,
            "index_list": indexes,
            "index_xinfo": index_xinfo,
        }
    return {"sqlite_master": objects, "tables": tables}


def _reference_snapshot(sql: str) -> dict[str, Any]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute("PRAGMA foreign_keys=ON")
        reference.execute("BEGIN IMMEDIATE")
        _execute_migration(reference, sql)
        result = _schema_snapshot(reference)
        reference.rollback()
        return result
    finally:
        reference.close()


@dataclass(frozen=True, slots=True)
class RepositoryTransaction:
    """Thread/lifetime-bound handle for explicitly composed outer mutations."""

    _repository_identity: int
    _thread_id: int
    _nonce: int


class SQLiteSkillRepository:
    """Skill authority using Core's transaction and committed-read contexts."""

    def __init__(self, authority: Any, *, asset_reader: Callable[[str], Any] | Any) -> None:
        if isinstance(authority, sqlite3.Connection):
            raise TypeError("bare SQLite connections are not an authoritative transaction owner")
        if not callable(getattr(authority, "transaction", None)) or not callable(getattr(authority, "read_connection", None)):
            raise TypeError("authority must provide transaction() and read_connection() contexts")
        if not callable(asset_reader) and not (
            callable(getattr(asset_reader, "read", None)) and callable(getattr(asset_reader, "require", None))
        ):
            raise TypeError("asset_reader must be the authoritative Core Asset reader")
        self._authority = authority
        self._asset_reader = asset_reader
        self._state_lock = threading.RLock()
        self._active_tokens: dict[int, tuple[int, sqlite3.Connection]] = {}
        self._nonce = 0
        self._savepoint = 0

    @contextmanager
    def transaction(self) -> Iterator[RepositoryTransaction]:
        """Open one authority transaction and issue a non-forgeable scoped token."""

        with self._state_lock:
            if self._active_tokens:
                raise RuntimeError("a Prompt Skill repository transaction is already active")
            thread_id = threading.get_ident()
            self._nonce += 1
            nonce = self._nonce
        with self._authority.transaction() as connection:
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("authority transaction did not yield sqlite3.Connection")
            token = RepositoryTransaction(id(self), thread_id, nonce)
            with self._state_lock:
                self._active_tokens[nonce] = (thread_id, connection)
            try:
                yield token
            finally:
                with self._state_lock:
                    self._active_tokens.pop(nonce, None)

    def _token_connection(self, token: RepositoryTransaction) -> sqlite3.Connection:
        if not isinstance(token, RepositoryTransaction) or token._repository_identity != id(self):
            raise RuntimeError("repository transaction token belongs to another repository")
        if token._thread_id != threading.get_ident():
            raise RuntimeError("repository transaction token belongs to another thread")
        with self._state_lock:
            active = self._active_tokens.get(token._nonce)
        if active is None or active[0] != token._thread_id:
            raise RuntimeError("repository transaction token is no longer active")
        return active[1]

    @contextmanager
    def _mutation(self, token: RepositoryTransaction | None = None) -> Iterator[sqlite3.Connection]:
        if token is None:
            with self.transaction() as owned, self._mutation(owned) as connection:
                yield connection
            return
        connection = self._token_connection(token)
        with self._state_lock:
            self._savepoint += 1
            name = f"p2_skill_sp_{self._savepoint}"
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield connection
            connection.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
            raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._state_lock:
            if self._active_tokens:
                raise RuntimeError("committed-only Skill reads cannot run inside an uncommitted repository transaction")
        with self._authority.read_connection() as connection:
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("authority read context did not yield sqlite3.Connection")
            yield connection

    def migrate(self) -> None:
        """Apply/verify the fixed migration atomically against a same-engine reference."""

        sql, digest = _migration_material()
        expected = _reference_snapshot(sql)
        with self._mutation() as connection:
            columns = [str(row[1]) for row in connection.execute('PRAGMA table_info("schema_migration")')]
            if not {"migration_id", "sha256"}.issubset(columns):
                raise RuntimeError("authoritative Core migration ledger is missing or incompatible")
            row = connection.execute(
                "SELECT sha256 FROM schema_migration WHERE migration_id=?", (_MIGRATION_ID,)
            ).fetchone()
            if row is not None and row[0] != digest:
                raise RuntimeError(f"migration hash mismatch: {_MIGRATION_ID}")
            if row is None:
                _execute_migration(connection, sql)
            actual = _schema_snapshot(connection)
            if actual != expected:
                raise RuntimeError("Prompt Skill schema differs from same-engine reference database")
            foreign_key_errors = [
                tuple(item)
                for item in connection.execute("PRAGMA foreign_key_check")
                if str(item[0]).startswith(_OWNED_PREFIX)
            ]
            if foreign_key_errors:
                raise RuntimeError(f"Prompt Skill schema has foreign-key violations: {foreign_key_errors}")
            if row is None:
                connection.execute(
                    "INSERT INTO schema_migration(migration_id,sha256) VALUES(?,?)", (_MIGRATION_ID, digest)
                )

    def _read_asset(self, asset_id: str, expected_hash: str) -> AssetRef:
        reader = self._asset_reader
        try:
            if callable(reader):
                value = reader(asset_id)
            else:
                reader.require(asset_id, sha256=expected_hash)
                value = reader.read(asset_id)
        except ContractError:
            raise
        except Exception as exc:
            raise _invalid(f"authoritative Asset is unavailable: {asset_id}", path="asset_id") from exc
        if isinstance(value, AssetRef):
            asset = value
        elif isinstance(value, (bytes, bytearray, memoryview, str)):
            asset = AssetRef.from_value(value, asset_id=asset_id)
        elif hasattr(value, "content"):
            asset = AssetRef(asset_id, bytes(value.content), getattr(value, "sha256", ""))
        else:
            raise _invalid("authoritative Asset reader returned an unsupported value", path="asset_id")
        if asset.asset_id != asset_id or asset.sha256 != expected_hash:
            raise _invalid("authoritative Asset identity/hash drift", path="asset_id")
        return asset

    def record_release(self, package: SkillPackage, *, token: RepositoryTransaction | None = None) -> None:
        package.verify_authoritative()
        manifest = thaw_json(package.manifest)
        files_manifest_hash = sha256_hex(package.identity.files_sha256)
        row = (
            package.skill_id,
            package.release_id,
            package.package_hash,
            package.version,
            _dump(manifest),
            files_manifest_hash,
        )
        with self._mutation(token) as connection:
            existing = connection.execute(
                "SELECT version,manifest_json,files_manifest_sha256,package_hash FROM p2_skill_release "
                "WHERE skill_id=? AND release_id=?",
                (package.skill_id, package.release_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (package.version, _dump(manifest), files_manifest_hash, package.package_hash):
                    raise _invalid("immutable Skill release identity was reused with different content")
                return
            connection.execute(
                "INSERT INTO p2_skill_release(skill_id,release_id,package_hash,version,manifest_json,files_manifest_sha256) "
                "VALUES(?,?,?,?,?,?)",
                row,
            )

    def set_active(
        self,
        *,
        skill_id: str,
        release_id: str,
        source: str,
        compare_and_swap: bool = False,
        expected_active_release_id: str | None = None,
        expected_revision: int | None = None,
        token: RepositoryTransaction | None = None,
    ) -> int:
        if source not in {"system", "user", "legacy"}:
            raise _invalid("active source is invalid", path="source")
        if source == "user" and (not compare_and_swap or expected_revision is None):
            raise _invalid("user active selection requires release and revision CAS")
        with self._mutation(token) as connection:
            release = connection.execute(
                "SELECT package_hash FROM p2_skill_release WHERE skill_id=? AND release_id=?",
                (skill_id, release_id),
            ).fetchone()
            if release is None:
                raise _invalid("cannot activate an unknown Skill release")
            current = connection.execute(
                "SELECT active_release_id,revision FROM p2_skill_active WHERE skill_id=?", (skill_id,)
            ).fetchone()
            if current is None:
                if compare_and_swap and (expected_active_release_id is not None or expected_revision not in {None, 0}):
                    raise _invalid("active Skill CAS base does not exist")
                connection.execute(
                    "INSERT INTO p2_skill_active(skill_id,active_release_id,package_hash,source,revision) VALUES(?,?,?,?,1)",
                    (skill_id, release_id, release[0], source),
                )
                return 1
            if compare_and_swap:
                cursor = connection.execute(
                    "UPDATE p2_skill_active SET active_release_id=?,package_hash=?,source=?,revision=revision+1 "
                    "WHERE skill_id=? AND active_release_id IS ? AND revision=?",
                    (release_id, release[0], source, skill_id, expected_active_release_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise _invalid("active Skill revision CAS failed")
            else:
                connection.execute(
                    "UPDATE p2_skill_active SET active_release_id=?,package_hash=?,source=?,revision=revision+1 WHERE skill_id=?",
                    (release_id, release[0], source, skill_id),
                )
            return int(current[1]) + 1

    def _preflight_chain(
        self,
        chain: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        *,
        expected_steps: Sequence[SkillStep],
        replay_context: Mapping[str, tuple[bytes | str, Mapping[str, bytes | str]]] | None,
        attribution_proofs: Mapping[str, AttributionProof],
    ) -> list[dict[str, Any]]:
        if not expected_steps or len(expected_steps) != len(receipts):
            raise _invalid("complete expected_steps are required for durable Skill receipts")
        verify_chain(chain, receipts, expected_steps=expected_steps, replay_context=replay_context)
        contexts: list[dict[str, Any]] = []
        for index, raw in enumerate(receipts):
            receipt = dict(raw)
            step = expected_steps[index]
            input_asset = self._read_asset(receipt["input_asset_id"], receipt["input_hash"])
            if step.parameters_asset_id is not None:
                if step.parameters_hash is None:
                    raise _invalid("frozen parameters Asset requires its hash")
                self._read_asset(step.parameters_asset_id, step.parameters_hash)
            if receipt["output_asset_id"] is not None:
                self._read_asset(receipt["output_asset_id"], receipt["output_hash"])
            replacement_hashes: dict[str, str] = {}
            for patch in receipt["patches"]:
                replacement_hashes[patch["replacement_asset_id"]] = patch["replacement_hash"]
                self._read_asset(patch["replacement_asset_id"], patch["replacement_hash"])
            proof = attribution_proofs.get(receipt["receipt_id"])
            attributed = receipt["participated"] or receipt["model_claimed"]
            if attributed and proof is None:
                raise _invalid("attributed Skill receipt lacks a validated ModelReceipt")
            if not attributed and proof is not None:
                raise _invalid("unattributed Skill receipt must not carry a ModelReceipt proof")
            proof_context: dict[str, Any] | None = None
            if proof is not None:
                if receipt["claim_evidence_asset_id"] != proof.asset_id:
                    raise _invalid("Skill claim evidence does not identify the validated ModelReceipt Asset")
                claim_asset = self._read_asset(proof.asset_id, proof.asset_hash)
                verified = verify_model_receipt_asset(claim_asset, invocation=proof.invocation)
                if verified.receipt.receipt_hash != proof.receipt.receipt_hash:
                    raise _invalid("ModelReceipt changed after attribution preflight")
                proof_context = {
                    "asset_id": proof.asset_id,
                    "asset_hash": proof.asset_hash,
                    "invocation": {
                        "invocation_id": proof.invocation.invocation_id,
                        "invocation_key": proof.invocation.invocation_key,
                        "request_hash": proof.invocation.request_hash,
                        "response_asset_id": proof.invocation.response_asset_id,
                        "response_hash": proof.invocation.response_hash,
                        "profile_revision_id": proof.invocation.profile_revision_id,
                        "provider_plugin_id": proof.invocation.provider_plugin_id,
                        "provider_release_id": proof.invocation.provider_release_id,
                        "input_context": thaw_json(proof.invocation.input_context),
                        "endpoint": proof.invocation.endpoint,
                        "model": proof.invocation.model,
                        "profile_revision": (
                            None
                            if proof.invocation.profile_revision is None
                            else thaw_json(proof.invocation.profile_revision)
                        ),
                        "max_retries": proof.invocation.max_retries,
                        "terminal_receipt_id": proof.invocation.terminal_receipt_id,
                        "terminal_receipt_hash": proof.invocation.terminal_receipt_hash,
                    },
                }
            contexts.append(
                {
                    "input": [input_asset.asset_id, input_asset.sha256],
                    "parameters": None if step.parameters_asset_id is None else [step.parameters_asset_id, step.parameters_hash],
                    "output": None if receipt["output_asset_id"] is None else [receipt["output_asset_id"], receipt["output_hash"]],
                    "replacements": replacement_hashes,
                    "model_receipt": proof_context,
                }
            )
        return contexts

    def record_chain(
        self,
        execution: ChainExecution | Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]] | None = None,
        *,
        expected_steps: Sequence[SkillStep],
        replay_context: Mapping[str, tuple[bytes | str, Mapping[str, bytes | str]]] | None = None,
        attribution_proofs: Mapping[str, AttributionProof] | None = None,
        token: RepositoryTransaction | None = None,
    ) -> None:
        if isinstance(execution, ChainExecution):
            chain = dict(execution.chain)
            receipt_values = [dict(item) for item in execution.receipts]
        else:
            chain = dict(execution)
            if receipts is None:
                raise TypeError("receipts are required when execution is a chain mapping")
            receipt_values = [dict(item) for item in receipts]
        proofs = dict(attribution_proofs or {})
        contexts = self._preflight_chain(
            chain,
            receipt_values,
            expected_steps=expected_steps,
            replay_context=replay_context,
            attribution_proofs=proofs,
        )
        with self._mutation(token) as connection:
            for step in expected_steps:
                row = connection.execute(
                    "SELECT 1 FROM p2_skill_release WHERE skill_id=? AND release_id=? AND package_hash=?",
                    (step.skill_id, step.release_id, step.package_hash),
                ).fetchone()
                if row is None:
                    raise _invalid("Skill receipt references a non-durable release")
            connection.execute(
                "INSERT INTO p2_skill_chain(chain_id,run_snapshot_hash,input_hash,final_output_hash,chain_hash,chain_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    chain["chain_id"],
                    chain["run_snapshot_hash"],
                    chain["input_hash"],
                    chain["final_output_hash"],
                    chain["chain_hash"],
                    _dump(chain),
                ),
            )
            for receipt, context in zip(receipt_values, contexts, strict=True):
                proof = proofs.get(receipt["receipt_id"])
                connection.execute(
                    "INSERT INTO p2_skill_receipt(receipt_id,chain_id,chain_index,skill_id,release_id,package_hash,"
                    "input_asset_id,input_hash,output_asset_id,output_hash,claim_asset_id,claim_asset_hash,receipt_hash,"
                    "receipt_json,validation_context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt["receipt_id"],
                        receipt["chain_id"],
                        receipt["chain_index"],
                        receipt["skill_id"],
                        receipt["release_id"],
                        receipt["package_hash"],
                        receipt["input_asset_id"],
                        receipt["input_hash"],
                        receipt["output_asset_id"],
                        receipt["output_hash"],
                        None if proof is None else proof.asset_id,
                        None if proof is None else proof.asset_hash,
                        receipt["receipt_hash"],
                        _dump(receipt),
                        _dump(context),
                    ),
                )

    def _read_and_verify(self, chain_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._read() as connection:
            chain_row = connection.execute(
                "SELECT chain_json FROM p2_skill_chain WHERE chain_id=?", (chain_id,)
            ).fetchone()
            if chain_row is None:
                raise KeyError(chain_id)
            rows = connection.execute(
                "SELECT r.receipt_json,r.validation_context_json,r.skill_id,r.release_id,r.package_hash,"
                "x.package_hash FROM p2_skill_receipt r JOIN p2_skill_release x "
                "ON x.skill_id=r.skill_id AND x.release_id=r.release_id AND x.package_hash=r.package_hash "
                "WHERE r.chain_id=? ORDER BY r.chain_index",
                (chain_id,),
            ).fetchall()
        chain = _load(chain_row[0])
        receipts: list[dict[str, Any]] = []
        expected_steps: list[SkillStep] = []
        replay_context: dict[str, tuple[bytes, Mapping[str, bytes]]] = {}
        for row in rows:
            receipt = _load(row[0])
            context = _load(row[1])
            expected_steps.append(
                SkillStep(
                    receipt["skill_id"],
                    receipt["release_id"],
                    receipt["package_hash"],
                    receipt["chain_index"] + 1,
                    receipt["parameters_asset_id"],
                    None if context["parameters"] is None else context["parameters"][1],
                )
            )
            input_asset = self._read_asset(*context["input"])
            if context["parameters"] is not None:
                self._read_asset(*context["parameters"])
            if context["output"] is not None:
                self._read_asset(*context["output"])
            replacements: dict[str, bytes] = {}
            for asset_id, asset_hash in context["replacements"].items():
                replacements[asset_id] = self._read_asset(asset_id, asset_hash).content
            if receipt["verified_patch"]:
                replay_context[receipt["receipt_id"]] = (input_asset.content, replacements)
            proof = context["model_receipt"]
            if proof is not None:
                invocation = FrozenModelInvocation(**proof["invocation"])
                claim_asset = self._read_asset(proof["asset_id"], proof["asset_hash"])
                verify_model_receipt_asset(claim_asset, invocation=invocation)
            elif receipt["participated"] or receipt["model_claimed"]:
                raise _invalid("durable attributed Skill receipt lost its ModelReceipt proof")
            receipts.append(receipt)
        if len(receipts) != len(rows):
            raise _invalid("durable Skill chain receipt set is incomplete")
        verify_chain(chain, receipts, expected_steps=expected_steps, replay_context=replay_context)
        return chain, receipts

    def load_chain(self, chain_id: str) -> ChainExecution:
        chain, receipts = self._read_and_verify(chain_id)
        return ChainExecution(chain, tuple(receipts))

    def skill_receipt_reader(self, chain_id: str) -> tuple[Mapping[str, Any], ...]:
        """ExecutionAuthority-compatible committed, revalidated receipt reader."""

        return tuple(self._read_and_verify(chain_id)[1])
