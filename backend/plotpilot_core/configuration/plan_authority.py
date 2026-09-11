"""Internal Plan registration, Workspace Plan CAS and planning availability."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk import (
    canonical_bytes,
    parse_model_config_v2,
    parse_model_profile_revision_v1,
    parse_project_planning_v2,
    validate_project_planning_start,
    validate_workspace_plan_selection,
)

from ..domain.entities import utc_now
from ..plugins.generation import validate_generation
from ..repositories.authority import ConflictError, CoreAuthorityRepository
from .authority import (
    CrossWorkspaceConfigurationError,
    DuplicateConfigurationOperationError,
    GenerationConfigurationConflictError,
    MalformedConfigurationRequestError,
    PlanningUnavailableError,
    StaleConfigurationCasError,
    UnknownConfigurationReferenceError,
    canonical_json,
    record_operation,
    replay_operation,
    request_fingerprint,
)

WORKSPACE_PLAN_SELECT_ROUTE = "workspace-plan.select"
PROJECT_PLANNING_GET_ROUTE = "project-planning.get"
PROJECT_PLANNING_START_ROUTE = "project-planning.start"

PROJECT_BRIEF_DOCUMENT_TYPE = "plotpilot.project-brief"
PROJECT_BRIEF_PAYLOAD_SCHEMA = "plotpilot.project-brief/v1"
PROJECT_PLANNER_PLUGIN_ID = "com.plotpilot.project-planner"
PROMPT_SKILL_PLUGIN_ID = "com.plotpilot.prompt-skill-runtime"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PlanRevisionReference:
    generation_id: str
    plan_revision_id: str
    plan_revision_hash: str
    registered_at: str


def _parse_selection_command(command: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = parse_model_config_v2(command)
    except Exception:
        raise MalformedConfigurationRequestError() from None
    if parsed.get("schema") != "workspace-plan-selection-command/v2":
        raise MalformedConfigurationRequestError()
    return parsed


def _parse_planning_command(
    command: Mapping[str, Any], expected_schema: str
) -> dict[str, Any]:
    try:
        parsed = parse_project_planning_v2(command)
    except Exception:
        raise MalformedConfigurationRequestError() from None
    if parsed.get("schema") != expected_schema:
        raise MalformedConfigurationRequestError()
    return parsed


def _generation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
        generation = validate_generation(payload)
        digest = hashlib.sha256(canonical_bytes(generation)).hexdigest()
    except Exception:
        raise GenerationConfigurationConflictError() from None
    if digest != row["payload_hash"]:
        raise GenerationConfigurationConflictError()
    return generation


class WorkspacePlanAuthority:
    """The only P1 writer for Workspace Plan selection history and pointer CAS."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        *,
        clock: Callable[[], str] = utc_now,
        selection_id_factory: Callable[[], str] | None = None,
    ) -> None:
        repository.ensure_model_configuration_schema()
        self.repository = repository
        self._clock = clock
        self._selection_id_factory = selection_id_factory or (
            lambda: f"plan-selection-{uuid.uuid4().hex}"
        )

    @staticmethod
    def _generation_for_update(
        connection: sqlite3.Connection, generation_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json,payload_hash FROM p2_plugin_generation "
            "WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise GenerationConfigurationConflictError()
        generation = _generation_from_row(row)
        if generation["generation_id"] != generation_id:
            raise GenerationConfigurationConflictError()
        return generation

    def register_verified_plan_reference(
        self,
        *,
        generation_id: str,
        plan_revision_id: str,
        plan_revision_hash: str,
    ) -> PlanRevisionReference:
        """Register one already-verified Plan tuple through an internal-only seam.

        No HTTP route calls this method.  Existence and canonical integrity of
        the referenced Generation are rechecked in the same Core transaction.
        """

        if (
            not isinstance(generation_id, str)
            or _ID.fullmatch(generation_id) is None
            or not isinstance(plan_revision_id, str)
            or _ID.fullmatch(plan_revision_id) is None
            or not isinstance(plan_revision_hash, str)
            or _HASH.fullmatch(plan_revision_hash) is None
        ):
            raise MalformedConfigurationRequestError()
        now = self._clock()
        with self.repository.transaction() as connection:
            self._generation_for_update(connection, generation_id)
            existing = connection.execute(
                "SELECT generation_id,plan_revision_id,plan_revision_hash,registered_at "
                "FROM p1_plan_revision_reference WHERE generation_id=? "
                "AND plan_revision_id=?",
                (generation_id, plan_revision_id),
            ).fetchone()
            if existing is not None:
                if existing["plan_revision_hash"] != plan_revision_hash:
                    raise GenerationConfigurationConflictError()
                return PlanRevisionReference(
                    str(existing["generation_id"]),
                    str(existing["plan_revision_id"]),
                    str(existing["plan_revision_hash"]),
                    str(existing["registered_at"]),
                )
            same_hash = connection.execute(
                "SELECT plan_revision_id FROM p1_plan_revision_reference "
                "WHERE generation_id=? AND plan_revision_hash=?",
                (generation_id, plan_revision_hash),
            ).fetchone()
            if same_hash is not None:
                raise GenerationConfigurationConflictError()
            connection.execute(
                "INSERT INTO p1_plan_revision_reference("
                "generation_id,plan_revision_id,plan_revision_hash,registered_at) "
                "VALUES(?,?,?,?)",
                (generation_id, plan_revision_id, plan_revision_hash, now),
            )
            return PlanRevisionReference(
                generation_id, plan_revision_id, plan_revision_hash, now
            )

    register_plan_reference = register_verified_plan_reference

    @staticmethod
    def _current_plan_hash(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        current_plan_revision_id: str | None,
    ) -> str | None:
        row = connection.execute(
            "SELECT plan_revision_id,plan_revision_hash "
            "FROM p1_workspace_plan_selection WHERE workspace_id=? "
            "ORDER BY workspace_revision DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if current_plan_revision_id is None:
            if row is not None:
                raise UnknownConfigurationReferenceError()
            return None
        if row is None or row["plan_revision_id"] != current_plan_revision_id:
            raise UnknownConfigurationReferenceError()
        return str(row["plan_revision_hash"])

    def select_workspace_plan(
        self, command: Mapping[str, Any]
    ) -> dict[str, Any]:
        parsed = _parse_selection_command(command)
        fingerprint = request_fingerprint(
            WORKSPACE_PLAN_SELECT_ROUTE,
            {"workspace_id": parsed["workspace_id"]},
            parsed,
        )
        now = self._clock()
        with self.repository.transaction() as connection:
            replay = replay_operation(
                connection,
                route_id=WORKSPACE_PLAN_SELECT_ROUTE,
                operation_key=parsed["operation_key"],
                fingerprint=fingerprint,
            )
            if replay is not None:
                if replay.status != 200:
                    raise RuntimeError("stored Plan selection status is invalid")
                try:
                    _, response = validate_workspace_plan_selection(
                        parsed, replay.response
                    )
                except Exception:
                    raise RuntimeError("stored Plan selection response is invalid") from None
                return response

            workspace = connection.execute(
                "SELECT workspace_id,current_plan_revision_id,revision FROM workspace "
                "WHERE workspace_id=?",
                (parsed["workspace_id"],),
            ).fetchone()
            if workspace is None:
                raise UnknownConfigurationReferenceError()
            actual_revision = int(workspace["revision"])
            actual_plan_id = workspace["current_plan_revision_id"]
            if actual_revision != parsed["expected_workspace_revision"]:
                raise StaleConfigurationCasError()
            if actual_plan_id != parsed["expected_current_plan_revision_id"]:
                raise StaleConfigurationCasError()
            actual_plan_hash = self._current_plan_hash(
                connection,
                workspace_id=parsed["workspace_id"],
                current_plan_revision_id=actual_plan_id,
            )
            if actual_plan_hash != parsed["expected_current_plan_revision_hash"]:
                raise StaleConfigurationCasError()

            pointer = connection.execute(
                "SELECT current_generation_id,safe_mode "
                "FROM p2_plugin_generation_pointer WHERE singleton=1"
            ).fetchone()
            if (
                pointer is None
                or bool(pointer["safe_mode"])
                or pointer["current_generation_id"] is None
                or pointer["current_generation_id"]
                != parsed["expected_active_generation_id"]
            ):
                raise GenerationConfigurationConflictError()
            active_generation_id = str(pointer["current_generation_id"])
            self._generation_for_update(connection, active_generation_id)

            plan_reference = connection.execute(
                "SELECT plan_revision_hash FROM p1_plan_revision_reference "
                "WHERE generation_id=? AND plan_revision_id=?",
                (active_generation_id, parsed["plan_revision_id"]),
            ).fetchone()
            if (
                plan_reference is None
                or plan_reference["plan_revision_hash"]
                != parsed["plan_revision_hash"]
            ):
                raise UnknownConfigurationReferenceError()

            profile = connection.execute(
                "SELECT payload_json,revision_hash FROM p1_model_profile_revision "
                "WHERE revision_id=?",
                (parsed["model_profile_revision_id"],),
            ).fetchone()
            if (
                profile is None
                or profile["revision_hash"]
                != parsed["model_profile_revision_hash"]
            ):
                raise UnknownConfigurationReferenceError()
            try:
                stored_profile = parse_model_profile_revision_v1(
                    json.loads(profile["payload_json"])
                )
            except Exception:
                raise UnknownConfigurationReferenceError() from None
            if stored_profile["revision_hash"] != profile["revision_hash"]:
                raise UnknownConfigurationReferenceError()

            try:
                validate_workspace_plan_selection(
                    parsed,
                    expected_workspace_revision=actual_revision,
                    expected_current_plan_revision_id=actual_plan_id,
                    expected_current_plan_revision_hash=actual_plan_hash,
                    active_generation_id=active_generation_id,
                )
            except Exception:
                raise RuntimeError("validated Plan selection lost authority binding") from None

            try:
                workspace_revision = self.repository.cas_workspace_plan_in_transaction(
                    connection,
                    workspace_id=parsed["workspace_id"],
                    expected_revision=actual_revision,
                    expected_current_plan_revision_id=actual_plan_id,
                    target_plan_revision_id=parsed["plan_revision_id"],
                    updated_at=now,
                )
            except ConflictError:
                raise StaleConfigurationCasError() from None
            if workspace_revision > 9_007_199_254_740_991:
                raise StaleConfigurationCasError()

            response = {
                "schema": "workspace-plan-selection-result/v2",
                "operation_key": parsed["operation_key"],
                "workspace_id": parsed["workspace_id"],
                "workspace_revision": workspace_revision,
                "previous_plan_revision_id": actual_plan_id,
                "previous_plan_revision_hash": actual_plan_hash,
                "selection_mode": "explicit",
                "plan_revision_id": parsed["plan_revision_id"],
                "plan_revision_hash": parsed["plan_revision_hash"],
                "model_profile_revision_id": parsed[
                    "model_profile_revision_id"
                ],
                "model_profile_revision_hash": parsed[
                    "model_profile_revision_hash"
                ],
                "active_generation_id": active_generation_id,
                "idempotent": False,
            }
            try:
                _, response = validate_workspace_plan_selection(parsed, response)
            except Exception:
                raise RuntimeError("server-generated Plan response is invalid") from None
            connection.execute(
                "INSERT INTO p1_workspace_plan_selection("
                "selection_id,operation_key,workspace_id,"
                "previous_plan_revision_id,previous_plan_revision_hash,"
                "plan_revision_id,plan_revision_hash,model_profile_revision_id,"
                "model_profile_revision_hash,active_generation_id,selection_mode,"
                "workspace_revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._selection_id_factory(),
                    parsed["operation_key"],
                    parsed["workspace_id"],
                    actual_plan_id,
                    actual_plan_hash,
                    parsed["plan_revision_id"],
                    parsed["plan_revision_hash"],
                    parsed["model_profile_revision_id"],
                    parsed["model_profile_revision_hash"],
                    active_generation_id,
                    "explicit",
                    workspace_revision,
                    now,
                ),
            )
            record_operation(
                connection,
                route_id=WORKSPACE_PLAN_SELECT_ROUTE,
                operation_key=parsed["operation_key"],
                fingerprint=fingerprint,
                value_hash=None,
                status=200,
                response=response,
                created_at=now,
            )
            return copy.deepcopy(response)

    select = select_workspace_plan

    @staticmethod
    def _project_brief(
        connection: sqlite3.Connection, workspace_id: str
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT d.document_id,d.current_revision_id,r.content_hash,"
            "r.payload_schema FROM document d LEFT JOIN revision r "
            "ON r.revision_id=d.current_revision_id AND r.document_id=d.document_id "
            "AND r.workspace_id=d.workspace_id "
            "WHERE d.workspace_id=? AND d.document_type=?",
            (workspace_id, PROJECT_BRIEF_DOCUMENT_TYPE),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        if (
            row["current_revision_id"] is None
            or row["payload_schema"] != PROJECT_BRIEF_PAYLOAD_SCHEMA
            or not isinstance(row["content_hash"], str)
            or _HASH.fullmatch(row["content_hash"]) is None
        ):
            return None
        return row

    @staticmethod
    def _availability_result(workspace_id: str, reason: str) -> dict[str, Any]:
        result = {
            "schema": "project-planning-availability-result/v2",
            "workspace_id": workspace_id,
            "available": reason == "ready",
            "reason": reason,
        }
        try:
            return parse_project_planning_v2(result)
        except Exception:
            raise RuntimeError("planning availability result is invalid") from None

    def planning_availability(
        self, query: Mapping[str, Any] | str
    ) -> dict[str, Any]:
        if isinstance(query, str):
            command: Mapping[str, Any] = {
                "schema": "project-planning-query/v2",
                "workspace_id": query,
            }
        else:
            command = query
        parsed = _parse_planning_command(command, "project-planning-query/v2")
        workspace_id = parsed["workspace_id"]
        with self.repository.read_connection() as connection:
            workspace = connection.execute(
                "SELECT current_plan_revision_id,revision FROM workspace "
                "WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise UnknownConfigurationReferenceError()
            if self._project_brief(connection, workspace_id) is None:
                return self._availability_result(
                    workspace_id, "project_brief_missing"
                )

            pointer = connection.execute(
                "SELECT current_generation_id,safe_mode "
                "FROM p2_plugin_generation_pointer WHERE singleton=1"
            ).fetchone()
            if (
                pointer is None
                or bool(pointer["safe_mode"])
                or pointer["current_generation_id"] is None
            ):
                return self._availability_result(
                    workspace_id, "active_generation_missing"
                )
            generation_id = str(pointer["current_generation_id"])
            generation_row = connection.execute(
                "SELECT payload_json,payload_hash FROM p2_plugin_generation "
                "WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if generation_row is None:
                return self._availability_result(
                    workspace_id, "active_generation_missing"
                )
            try:
                generation = _generation_from_row(generation_row)
            except GenerationConfigurationConflictError:
                return self._availability_result(
                    workspace_id, "active_generation_missing"
                )
            members = {item["plugin_id"]: item for item in generation["members"]}
            if PROJECT_PLANNER_PLUGIN_ID not in members:
                return self._availability_result(
                    workspace_id, "planner_package_missing"
                )

            selection = connection.execute(
                "SELECT * FROM p1_workspace_plan_selection WHERE workspace_id=? "
                "ORDER BY workspace_revision DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
            current_plan_id = workspace["current_plan_revision_id"]
            if (
                selection is None
                or current_plan_id is None
                or selection["plan_revision_id"] != current_plan_id
                or selection["active_generation_id"] != generation_id
            ):
                return self._availability_result(
                    workspace_id, "workspace_plan_missing"
                )
            plan_reference = connection.execute(
                "SELECT plan_revision_hash FROM p1_plan_revision_reference "
                "WHERE generation_id=? AND plan_revision_id=?",
                (generation_id, current_plan_id),
            ).fetchone()
            if (
                plan_reference is None
                or plan_reference["plan_revision_hash"]
                != selection["plan_revision_hash"]
            ):
                return self._availability_result(
                    workspace_id, "workspace_plan_missing"
                )

            profile = connection.execute(
                "SELECT payload_json,revision_hash,secret_id "
                "FROM p1_model_profile_revision WHERE revision_id=?",
                (selection["model_profile_revision_id"],),
            ).fetchone()
            if (
                profile is None
                or profile["revision_hash"]
                != selection["model_profile_revision_hash"]
            ):
                return self._availability_result(
                    workspace_id, "model_profile_missing"
                )
            try:
                profile_payload = parse_model_profile_revision_v1(
                    json.loads(profile["payload_json"])
                )
            except Exception:
                return self._availability_result(
                    workspace_id, "model_profile_missing"
                )
            if profile_payload["revision_hash"] != profile["revision_hash"]:
                return self._availability_result(
                    workspace_id, "model_profile_missing"
                )

            provider = profile_payload["provider"]
            provider_member = members.get(provider["plugin_id"])
            secret = connection.execute(
                "SELECT 1 FROM p1_local_secret_value WHERE secret_id=?",
                (profile["secret_id"],),
            ).fetchone()
            if (
                provider_member is None
                or provider_member["release_id"] != provider["release_id"]
                or secret is None
            ):
                return self._availability_result(
                    workspace_id, "provider_unavailable"
                )
            if PROMPT_SKILL_PLUGIN_ID not in members:
                return self._availability_result(
                    workspace_id, "prompt_skill_unavailable"
                )
            return self._availability_result(workspace_id, "ready")

    get_planning_availability = planning_availability

    def start_planning(self, command: Mapping[str, Any]) -> None:
        """Validate identity/CAS and remain effect-free until P2A is composed."""

        parsed = _parse_planning_command(
            command, "project-planning-start-command/v2"
        )
        with self.repository.read_connection() as connection:
            workspace = connection.execute(
                "SELECT 1 FROM workspace WHERE workspace_id=?",
                (parsed["workspace_id"],),
            ).fetchone()
            if workspace is None:
                raise UnknownConfigurationReferenceError()
            document = connection.execute(
                "SELECT workspace_id,document_type,current_revision_id FROM document "
                "WHERE document_id=?",
                (parsed["project_brief_document_id"],),
            ).fetchone()
            if document is None:
                raise UnknownConfigurationReferenceError()
            if document["workspace_id"] != parsed["workspace_id"]:
                raise CrossWorkspaceConfigurationError()
            if (
                document["document_type"] != PROJECT_BRIEF_DOCUMENT_TYPE
                or document["current_revision_id"] is None
            ):
                raise UnknownConfigurationReferenceError()
            revision = connection.execute(
                "SELECT revision_id,content_hash,payload_schema FROM revision "
                "WHERE revision_id=? AND document_id=? AND workspace_id=?",
                (
                    document["current_revision_id"],
                    parsed["project_brief_document_id"],
                    parsed["workspace_id"],
                ),
            ).fetchone()
            if revision is None or revision["payload_schema"] != PROJECT_BRIEF_PAYLOAD_SCHEMA:
                raise UnknownConfigurationReferenceError()
            authority = {
                "document_id": parsed["project_brief_document_id"],
                "revision_id": revision["revision_id"],
                "content_hash": revision["content_hash"],
            }
            try:
                validate_project_planning_start(
                    parsed, expected_project_brief=authority
                )
            except Exception:
                raise StaleConfigurationCasError() from None
        # No operation row, Job, RunSnapshot or Attempt is created in P1.
        raise PlanningUnavailableError()

    start = start_planning


PlanAuthority = WorkspacePlanAuthority


__all__ = [
    "PROJECT_BRIEF_DOCUMENT_TYPE",
    "PROJECT_BRIEF_PAYLOAD_SCHEMA",
    "PROJECT_PLANNER_PLUGIN_ID",
    "PROMPT_SKILL_PLUGIN_ID",
    "PlanAuthority",
    "PlanRevisionReference",
    "WorkspacePlanAuthority",
]
