"""SQLite ledger for reproducible translation benchmark runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from career_sim_runner.translation_benchmark.scoring import directional_error_summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Persist run metadata and each scored event immediately."""

    def __init__(self, path: Path) -> None:
        """Open or initialize the ledger at *path*."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    seed TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    dataset_root TEXT NOT NULL,
                    dictionary_sha256 TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    case_count INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    metric_correct INTEGER,
                    metric_total INTEGER,
                    option_correct INTEGER,
                    option_total INTEGER,
                    direction_score REAL,
                    direction_option_total INTEGER,
                    false_positive_count INTEGER,
                    false_negative_count INTEGER
                );
                CREATE TABLE IF NOT EXISTS results (
                    run_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    shorttitle TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    actual_json TEXT NOT NULL,
                    wrong_options_json TEXT NOT NULL,
                    mismatches_json TEXT NOT NULL,
                    errors_json TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    metric_correct INTEGER NOT NULL,
                    metric_total INTEGER NOT NULL,
                    option_correct INTEGER NOT NULL,
                    option_total INTEGER NOT NULL,
                    direction_score REAL,
                    direction_scores_json TEXT,
                    false_positive_count INTEGER,
                    false_negative_count INTEGER,
                    false_positive_metrics_json TEXT,
                    false_negative_metrics_json TEXT,
                    dictionary_activations_json TEXT,
                    PRIMARY KEY (run_id, position),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                """
            )
            self._ensure_column(database, "runs", "direction_score", "REAL")
            self._ensure_column(database, "runs", "direction_option_total", "INTEGER")
            self._ensure_column(database, "results", "direction_score", "REAL")
            self._ensure_column(database, "results", "direction_scores_json", "TEXT")
            self._ensure_column(database, "runs", "false_positive_count", "INTEGER")
            self._ensure_column(database, "runs", "false_negative_count", "INTEGER")
            self._ensure_column(database, "results", "false_positive_count", "INTEGER")
            self._ensure_column(database, "results", "false_negative_count", "INTEGER")
            self._ensure_column(database, "results", "false_positive_metrics_json", "TEXT")
            self._ensure_column(database, "results", "false_negative_metrics_json", "TEXT")
            for table in ("runs", "results"):
                for column in ("omission_count", "reversal_count"):
                    self._ensure_column(database, table, column, "INTEGER")
            for column in ("omission_metrics_json", "reversal_metrics_json"):
                self._ensure_column(database, "results", column, "TEXT")
            for column, definition in (
                ("mode", "TEXT"), ("dictionary_line_count", "INTEGER"),
                ("option_count", "INTEGER"), ("compression_rate", "REAL"),
                ("active_dictionary_lines", "INTEGER"),
                ("dictionary_event_activations", "INTEGER"),
                ("mean_activations_per_line", "REAL"),
                ("effect_review_count", "INTEGER"),
            ):
                self._ensure_column(database, "runs", column, definition)
            self._ensure_column(database, "results", "dictionary_activations_json", "TEXT")

    @staticmethod
    def _ensure_column(database: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        """Add one nullable score column when opening a legacy ledger."""
        columns = {str(row[1]) for row in database.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def begin(self, metadata: dict[str, Any]) -> None:
        """Insert a fresh run row."""
        with sqlite3.connect(self.path) as database:
            database.execute(
                """
                INSERT INTO runs (
                    run_id, seed, session_id, dataset_root, dictionary_sha256,
                    output_dir, case_count, started_at, mode, dictionary_line_count,
                    option_count, compression_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata["run_id"],
                    metadata["seed"],
                    metadata["session_id"],
                    metadata["dataset_root"],
                    metadata["dictionary_sha256"],
                    metadata["output_dir"],
                    metadata["case_count"],
                    _now(),
                    metadata.get("mode", "sample"),
                    metadata.get("dictionary_line_count"),
                    metadata.get("option_count"),
                    metadata.get("compression_rate"),
                ),
            )

    def case_ids(self) -> set[str]:
        """Return every case ID already recorded in this ledger."""
        with sqlite3.connect(self.path) as database:
            return {str(row[0]) for row in database.execute("SELECT DISTINCT case_id FROM results")}

    def resumable_run(self, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Load an unfinished run and its scored prefix without changing it."""
        with sqlite3.connect(self.path) as database:
            database.row_factory = sqlite3.Row
            run = database.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError(f"benchmark run not found: {run_id}")
            if run["completed_at"] is not None:
                raise ValueError(f"benchmark run already complete: {run_id}")
            rows = database.execute(
                "SELECT * FROM results WHERE run_id=? ORDER BY position", (run_id,)
            ).fetchall()
            return dict(run), [dict(row) for row in rows]

    def record(self, run_id: str, position: int, result: dict[str, Any]) -> None:
        """Persist one scored response."""

        def encode(value: object) -> str:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)

        with sqlite3.connect(self.path) as database:
            database.execute(
                """
                INSERT INTO results (
                    run_id, position, case_id, shorttitle, expected_json, actual_json,
                    wrong_options_json, mismatches_json, errors_json, response_text,
                    usage_json, metric_correct, metric_total, option_correct, option_total,
                    direction_score, direction_scores_json, false_positive_count,
                    false_negative_count, false_positive_metrics_json, false_negative_metrics_json,
                    dictionary_activations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    position,
                    result["case_id"],
                    result["shorttitle"],
                    encode(result["expected"]),
                    encode(result["actual"]),
                    encode(result["wrong_options"]),
                    encode(result["mismatches"]),
                    encode(result["errors"]),
                    result["response_text"],
                    encode(result["usage"]),
                    result["metric_correct"],
                    result["metric_total"],
                    result["option_correct"],
                    result["option_total"],
                    result["direction_score"],
                    encode(result["direction_scores"]),
                    result["false_positive_count"],
                    result["false_negative_count"],
                    encode(result["false_positive_metrics"]),
                    encode(result["false_negative_metrics"]),
                    encode(result.get("dictionary_activations", {})),
                ),
            )

            detail = directional_error_summary(result["expected"], result.get("predicted", result["actual"]))
            database.execute(
                "UPDATE results SET omission_count=?, reversal_count=?, omission_metrics_json=?, reversal_metrics_json=? WHERE run_id=? AND position=?",
                (detail["omission_count"], detail["reversal_count"], encode(detail["omission_metrics"]),
                 encode(detail["reversal_metrics"]), run_id, position),
            )

    def finish(self, run_id: str, totals: dict[str, int | float]) -> None:
        """Write final aggregate counts for a completed run."""
        with sqlite3.connect(self.path) as database:
            database.execute(
                """
                UPDATE runs SET completed_at=?, metric_correct=?, metric_total=?,
                    option_correct=?, option_total=?, direction_score=?,
                    direction_option_total=?, false_positive_count=?,
                    false_negative_count=?, active_dictionary_lines=?,
                    dictionary_event_activations=?, mean_activations_per_line=?,
                    effect_review_count=? WHERE run_id=?
                """,
                (
                    _now(),
                    totals["metric_correct"],
                    totals["metric_total"],
                    totals["option_correct"],
                    totals["option_total"],
                    totals["direction_score"],
                    totals["direction_option_total"],
                    totals["false_positive_count"],
                    totals["false_negative_count"],
                    totals.get("active_dictionary_lines"),
                    totals.get("dictionary_event_activations"),
                    totals.get("mean_activations_per_line"),
                    totals.get("effect_review_count"),
                    run_id,
                ),
            )

            database.execute(
                "UPDATE runs SET omission_count=(SELECT SUM(omission_count) FROM results WHERE run_id=?), "
                "reversal_count=(SELECT SUM(reversal_count) FROM results WHERE run_id=?) WHERE run_id=?",
                (run_id, run_id, run_id),
            )
