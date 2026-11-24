"""SQLite persistence for planning sessions, candidates and workflow events.

Standard-library ``sqlite3`` only. Every service operation runs in one
``BEGIN IMMEDIATE`` transaction, so the read-check-write sequence is
serialized across processes; updates additionally use optimistic version
checks (``WHERE version = ?``). All SQL is parameterized.
"""

import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from sabor_da_maria_agent.application.planning import (
    ApprovalRecord,
    PlanningSession,
    RecipeCandidate,
    StaleVersionError,
    WorkflowEvent,
)
from sabor_da_maria_agent.application.readiness import CandidateEvaluation
from sabor_da_maria_agent.domain.costing import SupplementalPurchaseQuote
from sabor_da_maria_agent.domain.kitchen import KitchenProfile
from sabor_da_maria_agent.domain.production import ProductionConfirmation
from sabor_da_maria_agent.domain.recipe import Recipe
from sabor_da_maria_agent.domain.workflow import CandidateStatus, SourceEvidence

DB_PATH_ENV = "SABOR_DA_MARIA_DB_PATH"
APP_DIR_NAME = "sabor-da-maria-agent"
DB_FILE_NAME = "planning.sqlite3"
SCHEMA_VERSION = 3

_EVIDENCE = TypeAdapter(tuple[SourceEvidence, ...])
_QUOTES = TypeAdapter(tuple[SupplementalPurchaseQuote, ...])
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in CandidateStatus)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS planning_sessions (
    session_id TEXT PRIMARY KEY CHECK (length(session_id) = 32),
    version INTEGER NOT NULL CHECK (version >= 1),
    kitchen_profile_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_candidates (
    candidate_id TEXT PRIMARY KEY CHECK (length(candidate_id) = 32),
    session_id TEXT NOT NULL
        REFERENCES planning_sessions (session_id) ON DELETE CASCADE,
    recipe_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ({_STATUS_VALUES})),
    recipe_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    quotes_json TEXT NOT NULL,
    allow_partial_cmv INTEGER NOT NULL CHECK (allow_partial_cmv IN (0, 1)),
    production_confirmation_json TEXT,
    evaluation_json TEXT,
    input_fingerprint TEXT,
    approval_json TEXT,
    calculation_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, recipe_id)
);

CREATE INDEX IF NOT EXISTS ix_candidates_session
    ON recipe_candidates (session_id);

CREATE TABLE IF NOT EXISTS workflow_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL
        REFERENCES planning_sessions (session_id) ON DELETE CASCADE,
    candidate_id TEXT
        REFERENCES recipe_candidates (candidate_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_events_session
    ON workflow_events (session_id, event_id);

CREATE INDEX IF NOT EXISTS ix_events_candidate_type
    ON workflow_events (candidate_id, event_type, event_id);
"""


def default_db_path() -> Path:
    """``SABOR_DA_MARIA_DB_PATH`` or a per-user application-data location."""
    configured = os.getenv(DB_PATH_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME / DB_FILE_NAME


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class _SQLiteUnitOfWork:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    # sessions ---------------------------------------------------------------
    def get_session(self, session_id: str) -> PlanningSession | None:
        row = self._db.execute(
            "SELECT * FROM planning_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        profile_json = row["kitchen_profile_json"]
        return PlanningSession(
            session_id=row["session_id"],
            version=row["version"],
            kitchen_profile=(
                KitchenProfile.model_validate_json(profile_json)
                if profile_json is not None
                else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def insert_session(self, session: PlanningSession) -> None:
        self._db.execute(
            "INSERT INTO planning_sessions (session_id, version, "
            "kitchen_profile_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.version,
                self._profile_json(session.kitchen_profile),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )

    def update_session(self, session: PlanningSession, expected_version: int) -> None:
        cursor = self._db.execute(
            "UPDATE planning_sessions SET version = ?, kitchen_profile_json = ?, "
            "updated_at = ? WHERE session_id = ? AND version = ?",
            (
                session.version,
                self._profile_json(session.kitchen_profile),
                session.updated_at.isoformat(),
                session.session_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleVersionError("Planning session was modified concurrently.")

    # candidates -------------------------------------------------------------
    def get_candidate(self, candidate_id: str) -> RecipeCandidate | None:
        row = self._db.execute(
            "SELECT * FROM recipe_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        return self._candidate(row) if row is not None else None

    def list_candidates(self, session_id: str) -> list[RecipeCandidate]:
        rows = self._db.execute(
            "SELECT * FROM recipe_candidates WHERE session_id = ? "
            "ORDER BY created_at, candidate_id",
            (session_id,),
        ).fetchall()
        return [self._candidate(row) for row in rows]

    def insert_candidate(self, candidate: RecipeCandidate) -> None:
        values = self._candidate_values(candidate)
        self._db.execute(
            "INSERT INTO recipe_candidates (candidate_id, session_id, recipe_id, "
            "version, status, recipe_json, evidence_json, quotes_json, "
            "allow_partial_cmv, production_confirmation_json, evaluation_json, "
            "input_fingerprint, approval_json, calculation_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.candidate_id,
                candidate.session_id,
                *values,
                candidate.created_at.isoformat(),
                candidate.updated_at.isoformat(),
            ),
        )

    def update_candidate(
        self, candidate: RecipeCandidate, expected_version: int
    ) -> None:
        cursor = self._db.execute(
            "UPDATE recipe_candidates SET recipe_id = ?, version = ?, status = ?, "
            "recipe_json = ?, evidence_json = ?, quotes_json = ?, "
            "allow_partial_cmv = ?, production_confirmation_json = ?, "
            "evaluation_json = ?, input_fingerprint = ?, approval_json = ?, "
            "calculation_json = ?, updated_at = ? "
            "WHERE candidate_id = ? AND session_id = ? AND version = ?",
            (
                *self._candidate_values(candidate),
                candidate.updated_at.isoformat(),
                candidate.candidate_id,
                candidate.session_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleVersionError("Recipe candidate was modified concurrently.")

    # events -----------------------------------------------------------------
    def add_event(self, event: WorkflowEvent) -> None:
        self._db.execute(
            "INSERT INTO workflow_events (session_id, candidate_id, event_type, "
            "from_status, to_status, version, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.session_id,
                event.candidate_id,
                event.event_type,
                event.from_status.value if event.from_status else None,
                event.to_status.value if event.to_status else None,
                event.version,
                _dump(event.payload),
                event.created_at.isoformat(),
            ),
        )

    def approval_sequence(self, candidate_id: str) -> int | None:
        row = self._db.execute(
            "SELECT event_id FROM workflow_events "
            "WHERE candidate_id = ? AND event_type = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (candidate_id, "candidate_approved"),
        ).fetchone()
        return int(row["event_id"]) if row is not None else None

    def list_events(
        self, session_id: str, candidate_id: str | None, limit: int
    ) -> list[WorkflowEvent]:
        limit = max(1, min(int(limit), 500))
        if candidate_id is None:
            rows = self._db.execute(
                "SELECT * FROM workflow_events WHERE session_id = ? "
                "ORDER BY event_id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM workflow_events WHERE session_id = ? "
                "AND candidate_id = ? ORDER BY event_id DESC LIMIT ?",
                (session_id, candidate_id, limit),
            ).fetchall()
        return [
            WorkflowEvent(
                session_id=row["session_id"],
                candidate_id=row["candidate_id"],
                event_type=row["event_type"],
                from_status=(
                    CandidateStatus(row["from_status"]) if row["from_status"] else None
                ),
                to_status=CandidateStatus(row["to_status"])
                if row["to_status"]
                else None,
                version=row["version"],
                payload=json.loads(row["payload_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in reversed(rows)
        ]

    # mapping ----------------------------------------------------------------
    @staticmethod
    def _profile_json(profile: KitchenProfile | None) -> str | None:
        return profile.model_dump_json() if profile is not None else None

    @staticmethod
    def _candidate_values(candidate: RecipeCandidate) -> tuple[Any, ...]:
        return (
            candidate.recipe.recipe_id,
            candidate.version,
            candidate.status.value,
            candidate.recipe.model_dump_json(),
            _EVIDENCE.dump_json(candidate.evidence).decode("utf-8"),
            _QUOTES.dump_json(candidate.quotes).decode("utf-8"),
            int(candidate.allow_partial_cmv),
            (
                candidate.production_confirmation.model_dump_json()
                if candidate.production_confirmation is not None
                else None
            ),
            candidate.evaluation.model_dump_json() if candidate.evaluation else None,
            candidate.input_fingerprint,
            candidate.approval.model_dump_json() if candidate.approval else None,
            _dump(candidate.calculation) if candidate.calculation is not None else None,
        )

    @staticmethod
    def _candidate(row: sqlite3.Row) -> RecipeCandidate:
        return RecipeCandidate(
            candidate_id=row["candidate_id"],
            session_id=row["session_id"],
            version=row["version"],
            status=CandidateStatus(row["status"]),
            recipe=Recipe.model_validate_json(row["recipe_json"]),
            evidence=_EVIDENCE.validate_json(row["evidence_json"]),
            quotes=_QUOTES.validate_json(row["quotes_json"]),
            allow_partial_cmv=bool(row["allow_partial_cmv"]),
            production_confirmation=(
                ProductionConfirmation.model_validate_json(
                    row["production_confirmation_json"]
                )
                if row["production_confirmation_json"]
                else None
            ),
            evaluation=(
                CandidateEvaluation.model_validate_json(row["evaluation_json"])
                if row["evaluation_json"]
                else None
            ),
            input_fingerprint=row["input_fingerprint"],
            approval=(
                ApprovalRecord.model_validate_json(row["approval_json"])
                if row["approval_json"]
                else None
            ),
            calculation=(
                json.loads(row["calculation_json"]) if row["calculation_json"] else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class SQLitePlanningRepository:
    """Connection-per-transaction repository; safe to share across threads."""

    def __init__(self, path: Path | str, *, timeout_seconds: float = 10.0) -> None:
        self._path = Path(path)
        self._timeout = timeout_seconds
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            # v2 introduced production confirmations; v3 binds those
            # confirmations to their targets and reserves pantry inventory.
            # Older approvals MUST be re-evaluated under these stronger gates.
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute("PRAGMA user_version").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database schema version {current} is newer than supported "
                        f"version {SCHEMA_VERSION}."
                    )
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(recipe_candidates)"
                    ).fetchall()
                }
                confirmation_column_missing = (
                    "production_confirmation_json" not in columns
                )
                if confirmation_column_missing:
                    connection.execute(
                        "ALTER TABLE recipe_candidates "
                        "ADD COLUMN production_confirmation_json TEXT"
                    )

                if current in (1, 2) or confirmation_column_missing:
                    # Existing decisions were made without v3 reservations
                    # and exact target-bound production confirmations.
                    # Keep history, but reject old evaluations and approvals.
                    now = datetime.now(UTC).isoformat()
                    stale_candidates = connection.execute(
                        "SELECT candidate_id, session_id, version, status "
                        "FROM recipe_candidates WHERE status != ?",
                        (CandidateStatus.DISCOVERED.value,),
                    ).fetchall()
                    for candidate in stale_candidates:
                        next_version = candidate["version"] + 1
                        connection.execute(
                            "UPDATE recipe_candidates SET version = ?, status = ?, "
                            "evaluation_json = NULL, input_fingerprint = NULL, "
                            "approval_json = NULL, calculation_json = NULL, "
                            "updated_at = ? WHERE candidate_id = ? AND version = ?",
                            (
                                next_version,
                                CandidateStatus.DISCOVERED.value,
                                now,
                                candidate["candidate_id"],
                                candidate["version"],
                            ),
                        )
                        connection.execute(
                            "INSERT INTO workflow_events (session_id, candidate_id, "
                            "event_type, from_status, to_status, version, "
                            "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                candidate["session_id"],
                                candidate["candidate_id"],
                                "candidate_invalidated_by_schema_migration",
                                candidate["status"],
                                CandidateStatus.DISCOVERED.value,
                                next_version,
                                _dump(
                                    {
                                        "reason": (
                                            "schema_v3_requires_stock_reservations_and_plan_binding"
                                        )
                                    }
                                ),
                                now,
                            ),
                        )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path, timeout=self._timeout, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[_SQLiteUnitOfWork]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield _SQLiteUnitOfWork(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
