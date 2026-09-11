"""Shared Audible library, persistent queue, and conversion service.

Direct compatibility calls and queued web/automatic work share the same download,
destination, decryption, conversion, and output-recording helpers.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

AUDIBLE_PROGRESS_PREFIX = "AUDIBLE_DOWNLOAD_PROGRESS="

TEMPLATE_FIELDS = {
    "author",
    "title",
    "subtitle",
    "series",
    "series_index",
    "series_index_padded",
    "year",
    "asin",
    "narrator",
}

STATUS_LABELS = {
    "not_processed": "Noch nicht verarbeitet",
    "file_exists": "Datei vorhanden",
    "history": "Laut Historie verarbeitet",
    "running": "Verarbeitung läuft",
    "error": "Fehler",
}

ACTIVE_JOB_STATUSES = (
    "queued",
    "downloading",
    "waiting_for_processing",
    "processing",
)

JOB_STATUS_LABELS = {
    "queued": "Wartet auf Download",
    "downloading": "Download läuft",
    "waiting_for_processing": "Wartet auf Verarbeitung",
    "processing": "Verarbeitung läuft",
    "completed": "Abgeschlossen",
    "failed": "Fehlgeschlagen",
    "cancelled": "Abgebrochen",
}

JOB_TYPE_LABELS = {
    "convert": "Konvertieren",
    "reconvert": "Erneut konvertieren",
    "normalize_existing": "Normalisieren",
}

SORT_FIELDS = {
    "title",
    "authors",
    "series_title",
    "purchase_date",
    "asin",
    "status",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_path_component(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "_", value or "")
    value = value.strip().strip(".")
    return value or "Audiobook"


def multipart_destination(base_destination: Path, part_number: int) -> Path:
    return base_destination.with_name(
        f"{base_destination.stem} - Part {part_number:02d}{base_destination.suffix}"
    )


@dataclass(frozen=True)
class ServiceConfig:
    config_directory: Path = Path("/config")
    audiobook_root: Path = Path("/audiobooks")
    work_root: Path = Path("/app/.audible-downloads")
    use_folders: bool = False
    single_book_path_template: str = "{author}/{title}/{title}.m4b"
    series_book_path_template: str = (
        "{author}/{series}/Band {series_index} - {title}/{title}.m4b"
    )
    normalize_target_i: float = -18.0
    normalize_target_tp: float = -1.5
    normalize_target_lra: float = 11.0

    @classmethod
    def from_environment(cls) -> ServiceConfig:
        return cls(
            config_directory=Path(os.getenv("AUDIBLE_CONFIG_DIR", "/config")),
            audiobook_root=Path(os.getenv("AUDIOBOOK_DIRECTORY", "/audiobooks")),
            work_root=Path(os.getenv("AUDIBLE_WORK_DIRECTORY", "/app/.audible-downloads")),
            use_folders=os.getenv("AUDIOBOOK_FOLDERS", "False") == "True",
            single_book_path_template=os.getenv(
                "SINGLE_BOOK_PATH_TEMPLATE", "{author}/{title}/{title}.m4b"
            ),
            series_book_path_template=os.getenv(
                "SERIES_BOOK_PATH_TEMPLATE",
                "{author}/{series}/Band {series_index} - {title}/{title}.m4b",
            ),
            normalize_target_i=float(os.getenv("AUDIO_NORMALIZE_TARGET_I", "-18")),
            normalize_target_tp=float(os.getenv("AUDIO_NORMALIZE_TARGET_TP", "-1.5")),
            normalize_target_lra=float(os.getenv("AUDIO_NORMALIZE_TARGET_LRA", "11")),
        )


@dataclass(frozen=True)
class ProcessResult:
    status: str
    run_id: int | None = None
    outputs: tuple[str, ...] = ()
    error: str | None = None

    @property
    def successful(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class EnqueueResult:
    job_id: int
    created: bool
    status: str
    error: str | None = None


class ProcessingError(RuntimeError):
    pass


class TargetExistsError(ProcessingError):
    pass


class AudibleService:
    def __init__(self, config: ServiceConfig | None = None):
        self.config = config or ServiceConfig.from_environment()
        self.database_path = self.config.config_directory / "audiobooks.db"
        self.config.config_directory.mkdir(parents=True, exist_ok=True)
        self.config.audiobook_root.mkdir(parents=True, exist_ok=True)
        self.config.work_root.mkdir(parents=True, exist_ok=True)
        self._book_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._download_lock = threading.Lock()
        self._media_lock = threading.Lock()
        self._worker_guard = threading.Lock()
        self._worker_stop = threading.Event()
        self._queue_wakeup = threading.Event()
        self._worker_threads: list[threading.Thread] = []
        self._activation_bytes_cache: str | None = None
        self.initialize_database()
        self.recover_waiting_jobs()
        self.cleanup_interrupted_work_directories()
        self.cleanup_normalization_temporaries()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize_database(self) -> None:
        """Create the current schema and add columns to legacy databases."""
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS audiobooks (
                    asin TEXT UNIQUE,
                    title TEXT NOT NULL,
                    subtitle TEXT,
                    authors TEXT NOT NULL,
                    series_title TEXT,
                    narrators TEXT,
                    series_sequence INT,
                    release_date TEXT,
                    downloaded INT DEFAULT 0,
                    purchase_date TEXT
                )"""
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(audiobooks)")
            }
            if "cover_url" not in columns:
                connection.execute("ALTER TABLE audiobooks ADD COLUMN cover_url TEXT")
            if "last_synced_at" not in columns:
                connection.execute("ALTER TABLE audiobooks ADD COLUMN last_synced_at TEXT")
            if "purchase_date" not in columns:
                connection.execute("ALTER TABLE audiobooks ADD COLUMN purchase_date TEXT")

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processing_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT NOT NULL,
                    requested_target_path TEXT NOT NULL,
                    target_overridden INT NOT NULL DEFAULT 0,
                    force INT NOT NULL DEFAULT 0,
                    overwrite INT NOT NULL DEFAULT 0,
                    trigger TEXT NOT NULL DEFAULT 'automatic',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    error_message TEXT,
                    FOREIGN KEY (asin) REFERENCES audiobooks(asin)
                );
                CREATE INDEX IF NOT EXISTS processing_runs_asin_id
                    ON processing_runs(asin, id DESC);
                CREATE TABLE IF NOT EXISTS output_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    part_number INTEGER,
                    FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE CASCADE,
                    UNIQUE(run_id, relative_path)
                );
                """
            )

            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(processing_runs)")
            }
            for name, declaration in (
                ("operation_type", "TEXT NOT NULL DEFAULT 'convert'"),
                ("normalization_requested", "INT NOT NULL DEFAULT 0"),
                ("normalization_status", "TEXT"),
                ("job_id", "INTEGER"),
            ):
                if name not in run_columns:
                    connection.execute(
                        f"ALTER TABLE processing_runs ADD COLUMN {name} {declaration}"
                    )

            output_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(output_files)")
            }
            if "normalization_status" not in output_columns:
                connection.execute(
                    "ALTER TABLE output_files ADD COLUMN normalization_status TEXT"
                )

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    asin TEXT,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    error_message TEXT,
                    force INT NOT NULL DEFAULT 0,
                    overwrite INT NOT NULL DEFAULT 0,
                    normalize INT NOT NULL DEFAULT 0,
                    requested_target_path TEXT,
                    target_overridden INT NOT NULL DEFAULT 0,
                    target_was_supplied INT NOT NULL DEFAULT 0,
                    downloaded_files TEXT,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    phase_label TEXT,
                    phase_progress_percent INTEGER,
                    dismissed_at TEXT,
                    dedupe_key TEXT NOT NULL,
                    run_id INTEGER,
                    FOREIGN KEY (asin) REFERENCES audiobooks(asin),
                    FOREIGN KEY (run_id) REFERENCES processing_runs(id)
                );
                CREATE INDEX IF NOT EXISTS jobs_status_id ON jobs(status, id);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_asin
                    ON jobs(asin)
                    WHERE status IN (
                        'queued', 'downloading', 'waiting_for_processing', 'processing'
                    );
                """
            )

            job_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            for name, declaration in (
                ("progress_percent", "INTEGER NOT NULL DEFAULT 0"),
                ("phase_label", "TEXT"),
                ("phase_progress_percent", "INTEGER"),
                ("dismissed_at", "TEXT"),
            ):
                if name not in job_columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")

            connection.execute(
                """UPDATE processing_runs
                   SET status='error', current_step='Fehler', finished_at=?,
                       error_message=COALESCE(error_message, 'Verarbeitung durch Neustart unterbrochen')
                   WHERE status='running'""",
                (utc_now(),),
            )
            interrupted = "Verarbeitung durch Neustart unterbrochen"
            active_jobs = connection.execute(
                """SELECT id, run_id FROM jobs
                   WHERE status IN ('downloading', 'processing')"""
            ).fetchall()
            for job in active_jobs:
                connection.execute(
                    """UPDATE jobs
                       SET status='failed', current_step='Unterbrochen', finished_at=?,
                           error_message=COALESCE(error_message, ?)
                       WHERE id=?""",
                    (utc_now(), interrupted, job["id"]),
                )
                if job["run_id"]:
                    connection.execute(
                        """UPDATE processing_runs
                           SET status='error', current_step='Unterbrochen', finished_at=?,
                               error_message=COALESCE(error_message, ?)
                           WHERE id=?""",
                        (utc_now(), interrupted, job["run_id"]),
                    )

    @staticmethod
    def _cover_url(row: dict[str, str]) -> str:
        for key in ("cover_url", "image_url", "picture", "cover"):
            if row.get(key):
                return row[key]
        product_images = row.get("product_images", "")
        if product_images:
            try:
                images = json.loads(product_images)
                if isinstance(images, dict):
                    for key in ("500", "300", "200", "1215"):
                        if images.get(key):
                            return images[key]
            except json.JSONDecodeError:
                pass
        return ""

    def sync_library_rows(self, rows: Iterable[dict[str, str]]) -> dict[str, int]:
        """Insert new Audible entries and update metadata without touching history."""
        added = 0
        updated = 0
        synced_at = utc_now()
        with self._connect() as connection:
            for row in rows:
                asin = (row.get("asin") or "").strip()
                title = (row.get("title") or "").strip()
                if not asin or not title:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM audiobooks WHERE asin=?", (asin,)
                ).fetchone()
                values = (
                    asin,
                    title,
                    row.get("subtitle") or "",
                    row.get("authors") or "",
                    row.get("series_title") or "",
                    row.get("narrators") or "",
                    row.get("series_sequence") or "",
                    row.get("release_date") or "",
                    self._cover_url(row),
                    synced_at,
                    row.get("purchase_date") or row.get("date_added") or "",
                )
                connection.execute(
                    """INSERT INTO audiobooks (
                           asin, title, subtitle, authors, series_title, narrators,
                           series_sequence, release_date, downloaded, cover_url,
                           last_synced_at, purchase_date
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                       ON CONFLICT(asin) DO UPDATE SET
                           title=excluded.title,
                           subtitle=excluded.subtitle,
                           authors=excluded.authors,
                           series_title=excluded.series_title,
                           narrators=excluded.narrators,
                           series_sequence=excluded.series_sequence,
                           release_date=excluded.release_date,
                           cover_url=CASE WHEN excluded.cover_url != ''
                                          THEN excluded.cover_url ELSE audiobooks.cover_url END,
                           last_synced_at=excluded.last_synced_at,
                           purchase_date=CASE WHEN excluded.purchase_date != ''
                                              THEN excluded.purchase_date
                                              ELSE audiobooks.purchase_date END""",
                    values,
                )
                if exists:
                    updated += 1
                else:
                    added += 1
        return {"added": added, "updated": updated}

    def refresh_library(self, library_path: Path | None = None) -> dict[str, int]:
        """Fetch and synchronize the library; never starts book processing."""
        temporary_directory = None
        if library_path is None:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="library-", dir=self.config.work_root
            )
            export_directory = Path(temporary_directory.name)
            subprocess.run(
                ["audible", "library", "export"],
                check=True,
                cwd=export_directory,
            )
            library_path = export_directory / "library.tsv"

        try:
            with Path(library_path).open(newline="", encoding="utf-8-sig") as file:
                result = self.sync_library_rows(csv.DictReader(file, delimiter="\t"))
        finally:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        return result

    def get_book(self, asin: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM audiobooks WHERE asin=?", (asin,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unbekannte ASIN: {asin}")
        return dict(row)

    @staticmethod
    def metadata_for_book(book: dict) -> dict[str, str]:
        release_date = book.get("release_date") or ""
        series_index = str(book.get("series_sequence") or "").strip()
        series_index_padded = (
            "0" + series_index if re.fullmatch(r"[0-9]", series_index) else series_index
        )
        return {
            "author": book.get("authors") or "",
            "title": book.get("title") or "",
            "series": book.get("series_title") or "",
            "subtitle": book.get("subtitle") or "",
            "narrator": book.get("narrators") or "",
            "series_index": series_index,
            "series_index_padded": series_index_padded,
            "year": release_date.split("-", 1)[0] if release_date else "",
            "asin": book.get("asin") or "",
        }

    def render_book_path(self, book_or_asin: dict | str) -> Path:
        book = self.get_book(book_or_asin) if isinstance(book_or_asin, str) else book_or_asin
        metadata = self.metadata_for_book(book)
        template = (
            self.config.series_book_path_template
            if metadata["series"]
            else self.config.single_book_path_template
        )
        fields = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template))
        unknown = sorted(fields - TEMPLATE_FIELDS)
        if unknown:
            unknown_text = ", ".join("{" + field + "}" for field in unknown)
            raise ValueError(f"Unbekannte Pfad-Platzhalter: {unknown_text}")

        safe_metadata = {
            key: safe_path_component(str(value)) if value else ""
            for key, value in metadata.items()
        }
        rendered = template.format_map(safe_metadata).replace("\\", "/")
        components = []
        for component in rendered.split("/"):
            component = component.strip()
            if not component or component in (".", ".."):
                continue
            components.append(safe_path_component(component))
        if not components:
            raise ValueError("Das Pfad-Template erzeugt einen leeren Pfad")
        if not components[-1].lower().endswith(".m4b"):
            components[-1] += ".m4b"
        return self.config.audiobook_root.joinpath(*components)

    def default_target_path(self, book_or_asin: dict | str) -> Path:
        book = self.get_book(book_or_asin) if isinstance(book_or_asin, str) else book_or_asin
        if self.config.use_folders:
            return self.render_book_path(book)
        filename = safe_path_component(f"{book['asin']} - {book['title']}") + ".m4b"
        return self.config.audiobook_root / filename

    def normalize_target_path(self, target_path: str | Path) -> Path:
        target = Path(target_path)
        if not target.is_absolute():
            target = self.config.audiobook_root / target
        if not target.name.lower().endswith(".m4b"):
            target = target.with_name(target.name + ".m4b")
        root = self.config.audiobook_root.resolve()
        resolved = target.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Der Zielpfad muss innerhalb von {root} liegen") from exc
        if resolved == root:
            raise ValueError("Der Zielpfad muss einen Dateinamen enthalten")
        return resolved

    def relative_output_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.config.audiobook_root.resolve()).as_posix()

    def existing_target_files(self, target_path: str | Path) -> list[Path]:
        target = self.normalize_target_path(target_path)
        matches = []
        if target.is_file():
            matches.append(target)
        if target.parent.is_dir():
            pattern = re.compile(
                rf"^{re.escape(target.stem)} - Part \d+{re.escape(target.suffix)}$",
                re.IGNORECASE,
            )
            matches.extend(
                path for path in target.parent.iterdir() if path.is_file() and pattern.match(path.name)
            )
        return sorted(set(matches))

    def discover_existing_m4b_files(
        self, asin: str, query: str = "", limit: int = 500
    ) -> list[dict]:
        """List real M4B files and their latest successful database association."""
        self.get_book(asin)  # Validate the ASIN before scanning the audiobook volume.
        with self._connect() as connection:
            association_rows = connection.execute(
                """SELECT o.relative_path, r.asin, a.title
                   FROM output_files o
                   JOIN processing_runs r ON r.id=o.run_id
                   LEFT JOIN audiobooks a ON a.asin=r.asin
                   WHERE r.status='success'
                   ORDER BY r.id DESC, o.id DESC"""
            ).fetchall()
        associations = {}
        for row in association_rows:
            associations.setdefault(
                row["relative_path"],
                {"asin": row["asin"], "title": row["title"] or row["asin"]},
            )

        query_text = query.strip().casefold()
        files = []
        for path in self.config.audiobook_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".m4b":
                continue
            if re.match(
                r"^\..*\.audible-normalize-.*\.tmp\.m4b$", path.name, re.IGNORECASE
            ):
                continue
            try:
                relative_path = self.relative_output_path(path)
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            if query_text and query_text not in relative_path.casefold():
                continue
            association = associations.get(relative_path)
            files.append(
                {
                    "relative_path": relative_path,
                    "absolute_path": str(path.resolve()),
                    "size": size,
                    "linked_asin": association["asin"] if association else None,
                    "linked_title": association["title"] if association else None,
                    "selectable": not association or association["asin"] == asin,
                    "selected": bool(association and association["asin"] == asin),
                }
            )
        files.sort(key=lambda item: item["relative_path"].casefold())
        return files[: max(1, limit)]

    def link_existing_outputs(
        self, book: dict | str, paths: Iterable[str | Path], trigger: str = "web"
    ) -> ProcessResult:
        """Associate existing M4Bs without modifying or processing their contents."""
        book_data = self.get_book(book) if isinstance(book, str) else book
        asin = book_data["asin"]
        if self.book_status(asin)["key"] == "running" or self._book_lock(asin).locked():
            return ProcessResult(
                "error", error="Dieses Hörbuch wird gerade verarbeitet."
            )

        normalized_paths = []
        seen = set()
        try:
            for raw_path in paths:
                if not str(raw_path).strip():
                    continue
                requested_path = str(raw_path).strip()
                if Path(requested_path).suffix.lower() != ".m4b":
                    raise ProcessingError(
                        f"Nur M4B-Dateien können verknüpft werden: {requested_path}"
                    )
                path = self.normalize_target_path(requested_path)
                relative_path = self.relative_output_path(path)
                if relative_path in seen:
                    continue
                if not path.is_file() or path.stat().st_size == 0:
                    raise ProcessingError(f"M4B-Datei fehlt oder ist leer: {path}")
                seen.add(relative_path)
                normalized_paths.append(path)
        except (OSError, ValueError, ProcessingError) as exc:
            return ProcessResult("error", error=str(exc))

        if not normalized_paths:
            return ProcessResult("error", error="Mindestens eine M4B-Datei auswählen.")

        normalized_paths.sort(key=lambda path: self.relative_output_path(path).casefold())
        now = utc_now()
        relative_paths = [self.relative_output_path(path) for path in normalized_paths]
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in relative_paths)
                association_rows = connection.execute(
                    f"""SELECT o.relative_path, o.normalization_status,
                               r.asin, a.title
                        FROM output_files o
                        JOIN processing_runs r ON r.id=o.run_id
                        LEFT JOIN audiobooks a ON a.asin=r.asin
                        WHERE r.status='success'
                          AND o.relative_path IN ({placeholders})
                        ORDER BY r.id DESC""",
                    relative_paths,
                ).fetchall()
                latest_associations = {}
                for row in association_rows:
                    latest_associations.setdefault(row["relative_path"], row)
                conflicts = [
                    row for row in latest_associations.values() if row["asin"] != asin
                ]
                if conflicts:
                    conflict = conflicts[0]
                    raise ProcessingError(
                        f"{conflict['relative_path']} ist bereits mit "
                        f"{conflict['title'] or conflict['asin']} verknüpft."
                    )
                normalization_by_path = {
                    relative_path: (
                        latest_associations[relative_path]["normalization_status"]
                        if relative_path in latest_associations
                        else None
                    )
                    for relative_path in relative_paths
                }
                normalization_states = set(normalization_by_path.values())
                if len(normalization_states) == 1:
                    run_normalization_status = next(iter(normalization_states))
                elif "normalized" in normalization_states:
                    run_normalization_status = "partial"
                else:
                    run_normalization_status = None
                cursor = connection.execute(
                    """INSERT INTO processing_runs (
                           asin, requested_target_path, target_overridden, force,
                           overwrite, trigger, started_at, finished_at, status,
                           current_step, operation_type, normalization_requested,
                           normalization_status
                       ) VALUES (?, ?, 1, 0, 0, ?, ?, ?, 'success',
                                 'Bestehende Dateien verknüpft', 'link_existing', 0, ?)""",
                    (
                        asin,
                        relative_paths[0],
                        trigger,
                        now,
                        now,
                        run_normalization_status,
                    ),
                )
                run_id = cursor.lastrowid
                multipart = len(normalized_paths) > 1
                for index, relative_path in enumerate(relative_paths, start=1):
                    connection.execute(
                        """INSERT INTO output_files (
                               run_id, relative_path, part_number,
                               normalization_status
                           ) VALUES (?, ?, ?, ?)""",
                        (
                            run_id,
                            relative_path,
                            index if multipart else None,
                            normalization_by_path[relative_path],
                        ),
                    )
                connection.execute(
                    "UPDATE audiobooks SET downloaded=1 WHERE asin=?", (asin,)
                )
        except (sqlite3.Error, ProcessingError) as exc:
            return ProcessResult("error", error=str(exc))

        LOGGER.info(
            "Linked %s existing output file(s) to %s in run %s",
            len(relative_paths),
            asin,
            run_id,
        )
        return ProcessResult("success", run_id, tuple(relative_paths))

    def _latest_run(self, connection: sqlite3.Connection, asin: str, status=None):
        query = "SELECT * FROM processing_runs WHERE asin=?"
        parameters: list[object] = [asin]
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY id DESC LIMIT 1"
        return connection.execute(query, parameters).fetchone()

    def _outputs_for_run(self, connection: sqlite3.Connection, run_id: int) -> list[dict]:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM output_files WHERE run_id=? ORDER BY COALESCE(part_number, 0), id",
                (run_id,),
            )
        ]

    @staticmethod
    def _normalization_summary(outputs: list[dict]) -> dict[str, str]:
        states = {output.get("normalization_status") for output in outputs}
        if outputs and states == {"normalized"}:
            return {"key": "normalized", "label": "Normalisiert"}
        if outputs and states == {"not_normalized"}:
            return {"key": "not_normalized", "label": "Nicht normalisiert"}
        if "normalized" in states:
            return {"key": "partial", "label": "Teilweise normalisiert"}
        return {"key": "unknown", "label": "Normalisierung unbekannt"}

    def book_status(self, asin: str) -> dict:
        book = self.get_book(asin)
        with self._connect() as connection:
            latest = self._latest_run(connection, asin)
            latest_success = self._latest_run(connection, asin, "success")
            active_job = connection.execute(
                """SELECT * FROM jobs WHERE asin=? AND status IN (?, ?, ?, ?)
                   ORDER BY id LIMIT 1""",
                (asin, *ACTIVE_JOB_STATUSES),
            ).fetchone()
            outputs = (
                self._outputs_for_run(connection, latest_success["id"])
                if latest_success
                else []
            )

        output_state = []
        for output in outputs:
            absolute = self.config.audiobook_root / output["relative_path"]
            try:
                exists = absolute.is_file() and absolute.stat().st_size > 0
            except OSError:
                exists = False
            output_state.append(
                {
                    **output,
                    "absolute_path": str(absolute),
                    "exists": exists,
                }
            )

        if active_job or (latest and latest["status"] in ("queued", "running")):
            key = "running"
        elif latest and latest["status"] == "error":
            key = "error"
        elif latest_success and output_state and all(item["exists"] for item in output_state):
            key = "file_exists"
        elif latest_success or book.get("downloaded"):
            key = "history"
        else:
            key = "not_processed"

        return {
            "key": key,
            "label": STATUS_LABELS[key],
            "current_step": (
                active_job["current_step"]
                if active_job
                else latest["current_step"] if latest else None
            ),
            "progress_percent": (
                active_job["progress_percent"] if active_job else None
            ),
            "phase_label": active_job["phase_label"] if active_job else None,
            "phase_progress_percent": (
                active_job["phase_progress_percent"] if active_job else None
            ),
            "error_message": (
                latest["error_message"]
                if latest and latest["status"] == "error"
                else None
            ),
            "has_success_history": bool(latest_success or book.get("downloaded")),
            "outputs": output_state,
            "all_outputs_exist": bool(output_state)
            and all(item["exists"] for item in output_state),
            "normalization": self._normalization_summary(output_state),
            "active_job": dict(active_job) if active_job else None,
            "latest_run": dict(latest) if latest else None,
        }

    def list_books(
        self,
        query: str = "",
        status_filter: str = "",
        sort_key: str = "purchase_date",
        sort_direction: str = "desc",
    ) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audiobooks ORDER BY title COLLATE NOCASE, asin"
            ).fetchall()
        if sort_key not in SORT_FIELDS:
            sort_key = "purchase_date"
        descending = sort_direction == "desc"
        words = query.casefold().split()
        books = []
        for row in rows:
            book = dict(row)
            searchable = " ".join(
                str(book.get(field) or "")
                for field in ("title", "subtitle", "authors", "series_title", "asin")
            ).casefold()
            if words and not all(word in searchable for word in words):
                continue
            book["status"] = self.book_status(book["asin"])
            if status_filter and book["status"]["key"] != status_filter:
                continue
            books.append(book)

        def sort_value(book: dict) -> str:
            if sort_key == "status":
                value = book["status"]["label"]
            else:
                value = book.get(sort_key)
            return str(value or "").casefold()

        # Missing dates/metadata remain at the end in either direction.
        populated = [book for book in books if sort_value(book)]
        missing = [book for book in books if not sort_value(book)]
        populated.sort(
            key=lambda book: (sort_value(book), str(book["asin"]).casefold()),
            reverse=descending,
        )
        return populated + missing

    def _book_lock(self, asin: str) -> threading.Lock:
        with self._locks_guard:
            return self._book_locks.setdefault(asin, threading.Lock())

    def _create_run(
        self,
        asin: str,
        target: Path,
        target_overridden: bool,
        force: bool,
        overwrite: bool,
        trigger: str,
        status: str = "running",
        operation_type: str = "convert",
        normalize: bool = False,
        job_id: int | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO processing_runs (
                       asin, requested_target_path, target_overridden, force, overwrite,
                       trigger, started_at, status, current_step, operation_type,
                       normalization_requested, normalization_status, job_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    asin,
                    self.relative_output_path(target),
                    int(target_overridden),
                    int(force),
                    int(overwrite),
                    trigger,
                    utc_now(),
                    status,
                    (
                        "Download wird vorbereitet"
                        if status == "running"
                        else "Eingereiht" if status == "queued" else "Übersprungen"
                    ),
                    operation_type,
                    int(normalize),
                    "pending" if normalize else "not_normalized",
                    job_id,
                ),
            )
            return cursor.lastrowid

    def _set_run_step(self, run_id: int, step: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE processing_runs SET current_step=? WHERE id=?", (step, run_id)
            )

    def _finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        step = {
            "success": "Fertig",
            "skipped": "Übersprungen",
            "cancelled": "Abgebrochen",
        }.get(status, "Fehler")
        with self._connect() as connection:
            connection.execute(
                """UPDATE processing_runs
                   SET status=?, current_step=?, finished_at=?, error_message=?,
                       normalization_status=CASE
                           WHEN normalization_requested=1 AND ?='success' THEN 'normalized'
                           WHEN normalization_requested=1 AND ?='error' THEN 'failed'
                           ELSE normalization_status END
                   WHERE id=?""",
                (status, step, utc_now(), error, status, status, run_id),
            )

    def _record_output(
        self,
        run_id: int,
        path: Path,
        part_number: int | None,
        normalization_status: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO output_files (
                       run_id, relative_path, part_number, normalization_status
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id, relative_path) DO UPDATE SET
                       part_number=excluded.part_number,
                       normalization_status=COALESCE(
                           excluded.normalization_status,
                           output_files.normalization_status
                       )""",
                (
                    run_id,
                    self.relative_output_path(path),
                    part_number,
                    normalization_status,
                ),
            )

    def _enqueue_job(
        self,
        *,
        book: dict,
        job_type: str,
        target: Path,
        target_overridden: bool,
        target_was_supplied: bool,
        force: bool,
        overwrite: bool,
        normalize: bool,
        trigger: str,
        initial_status: str,
        output_paths: list[tuple[Path, int | None, str | None]] | None = None,
    ) -> EnqueueResult:
        asin = book["asin"]
        created_at = utc_now()
        relative_target = self.relative_output_path(target)
        dedupe_key = "|".join(
            (
                job_type,
                asin,
                relative_target,
                str(int(force)),
                str(int(overwrite)),
                str(int(normalize)),
            )
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT id, status FROM jobs
                   WHERE asin=? AND status IN (?, ?, ?, ?) ORDER BY id LIMIT 1""",
                (asin, *ACTIVE_JOB_STATUSES),
            ).fetchone()
            if existing:
                return EnqueueResult(
                    existing["id"],
                    False,
                    existing["status"],
                    "Für dieses Hörbuch ist bereits ein Job eingereiht oder aktiv.",
                )

            cursor = connection.execute(
                """INSERT INTO jobs (
                       job_type, asin, title, created_at, status, current_step,
                       force, overwrite, normalize, requested_target_path,
                       target_overridden, target_was_supplied, dedupe_key
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_type,
                    asin,
                    book["title"],
                    created_at,
                    initial_status,
                    (
                        "Wartet auf Verarbeitung"
                        if initial_status == "waiting_for_processing"
                        else "Wartet auf Download"
                    ),
                    int(force),
                    int(overwrite),
                    int(normalize),
                    relative_target,
                    int(target_overridden),
                    int(target_was_supplied),
                    dedupe_key,
                ),
            )
            job_id = cursor.lastrowid
            run_cursor = connection.execute(
                """INSERT INTO processing_runs (
                       asin, requested_target_path, target_overridden, force,
                       overwrite, trigger, started_at, status, current_step,
                       operation_type, normalization_requested,
                       normalization_status, job_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                (
                    asin,
                    relative_target,
                    int(target_overridden),
                    int(force),
                    int(overwrite),
                    trigger,
                    created_at,
                    (
                        "Wartet auf Verarbeitung"
                        if initial_status == "waiting_for_processing"
                        else "Wartet auf Download"
                    ),
                    job_type,
                    int(normalize),
                    "pending" if normalize else "not_normalized",
                    job_id,
                ),
            )
            run_id = run_cursor.lastrowid
            connection.execute(
                "UPDATE jobs SET run_id=? WHERE id=?", (run_id, job_id)
            )
            for path, part_number, normalization_status in output_paths or []:
                connection.execute(
                    """INSERT INTO output_files (
                           run_id, relative_path, part_number, normalization_status
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        run_id,
                        self.relative_output_path(path),
                        part_number,
                        normalization_status,
                    ),
                )
        self._queue_wakeup.set()
        LOGGER.info("[job %s] queued %s for %s", job_id, job_type, asin)
        return EnqueueResult(job_id, True, initial_status)

    def enqueue_conversion(
        self,
        book: dict | str,
        target_path: str | Path | None = None,
        force: bool = False,
        overwrite: bool = False,
        normalize: bool = False,
        trigger: str = "web",
    ) -> EnqueueResult:
        """Persist a conversion request and return without doing network or media work."""
        book_data = self.get_book(book) if isinstance(book, str) else book
        default_target = self.default_target_path(book_data)
        target = self.normalize_target_path(target_path or default_target)
        current_status = self.book_status(book_data["asin"])
        if current_status["key"] == "running" or self._book_lock(
            book_data["asin"]
        ).locked():
            active = current_status.get("active_job")
            return EnqueueResult(
                active["id"] if active else 0,
                False,
                active["status"] if active else "processing",
                "Für dieses Hörbuch ist bereits eine Verarbeitung aktiv.",
            )
        if current_status["has_success_history"] and not force:
            return EnqueueResult(
                0, False, "skipped", "Bereits verarbeitet; Force ist erforderlich"
            )
        return self._enqueue_job(
            book=book_data,
            job_type="reconvert" if force else "convert",
            target=target,
            target_overridden=target != self.normalize_target_path(default_target),
            target_was_supplied=target_path is not None,
            force=force,
            overwrite=overwrite,
            normalize=normalize,
            trigger=trigger,
            initial_status="queued",
        )

    def enqueue_normalize_existing(
        self, book: dict | str, trigger: str = "web"
    ) -> EnqueueResult:
        """Queue the exact output paths stored for the latest successful run."""
        book_data = self.get_book(book) if isinstance(book, str) else book
        current_status = self.book_status(book_data["asin"])
        if current_status["key"] == "running" or self._book_lock(
            book_data["asin"]
        ).locked():
            active = current_status.get("active_job")
            return EnqueueResult(
                active["id"] if active else 0,
                False,
                active["status"] if active else "processing",
                "Für dieses Hörbuch ist bereits eine Verarbeitung aktiv.",
            )
        with self._connect() as connection:
            latest_success = self._latest_run(connection, book_data["asin"], "success")
            rows = (
                self._outputs_for_run(connection, latest_success["id"])
                if latest_success
                else []
            )
        if not rows:
            return EnqueueResult(
                0, False, "failed", "Keine gespeicherten Ausgabedateien vorhanden."
            )
        output_paths = []
        for row in rows:
            path = self.normalize_target_path(row["relative_path"])
            if not path.is_file() or path.stat().st_size == 0:
                return EnqueueResult(
                    0, False, "failed", f"Gespeicherte Ausgabedatei fehlt: {path}"
                )
            output_paths.append(
                (path, row["part_number"], row.get("normalization_status"))
            )
        return self._enqueue_job(
            book=book_data,
            job_type="normalize_existing",
            target=output_paths[0][0],
            target_overridden=True,
            target_was_supplied=True,
            force=False,
            overwrite=True,
            normalize=True,
            trigger=trigger,
            initial_status="waiting_for_processing",
            output_paths=output_paths,
        )

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM jobs
                   WHERE dismissed_at IS NULL
                   ORDER BY CASE WHEN status IN (?, ?, ?, ?) THEN 0 ELSE 1 END,
                            CASE WHEN status IN (?, ?, ?, ?) THEN id END ASC,
                            CASE WHEN status NOT IN (?, ?, ?, ?) THEN id END DESC
                   LIMIT ?""",
                (
                    *ACTIVE_JOB_STATUSES,
                    *ACTIVE_JOB_STATUSES,
                    *ACTIVE_JOB_STATUSES,
                    limit,
                ),
            ).fetchall()
        jobs = [dict(row) for row in rows]
        position = 0
        for job in jobs:
            if job["status"] in ACTIVE_JOB_STATUSES:
                position += 1
                job["queue_position"] = position
            else:
                job["queue_position"] = None
            job["status_label"] = JOB_STATUS_LABELS.get(job["status"], job["status"])
            job["type_label"] = JOB_TYPE_LABELS.get(job["job_type"], job["job_type"])
        return jobs

    def dismiss_job(self, job_id: int) -> bool:
        """Hide a terminal job from the queue view while retaining its history."""
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE jobs SET dismissed_at=?
                   WHERE id=? AND status IN ('completed', 'failed', 'cancelled')
                     AND dismissed_at IS NULL""",
                (utc_now(), job_id),
            ).rowcount
        if changed:
            LOGGER.info("[job %s] dismissed from job list", job_id)
        return bool(changed)

    def cancel_job(self, job_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job or job["status"] not in ("queued", "waiting_for_processing"):
                return False
            now = utc_now()
            connection.execute(
                """UPDATE jobs SET status='cancelled', current_step='Abgebrochen',
                   finished_at=? WHERE id=?""",
                (now, job_id),
            )
            connection.execute(
                """UPDATE processing_runs
                   SET status='cancelled', current_step='Abgebrochen', finished_at=?
                   WHERE id=?""",
                (now, job["run_id"]),
            )
        LOGGER.info("[job %s] cancelled while waiting", job_id)
        return True

    def _claim_job(self, from_status: str, to_status: str, step: str) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY id LIMIT 1",
                (from_status,),
            ).fetchone()
            if not job:
                return None
            now = utc_now()
            changed = connection.execute(
                """UPDATE jobs SET status=?, current_step=?,
                   started_at=COALESCE(started_at, ?)
                   WHERE id=? AND status=?""",
                (to_status, step, now, job["id"], from_status),
            ).rowcount
            if not changed:
                return None
            connection.execute(
                """UPDATE processing_runs SET status='running', current_step=?,
                   started_at=? WHERE id=?""",
                (step, now, job["run_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            return dict(claimed)

    def _update_job_step(
        self, job_id: int, step: str, progress_percent: int | None = None
    ) -> None:
        progress = (
            max(0, min(100, int(progress_percent)))
            if progress_percent is not None
            else None
        )
        with self._connect() as connection:
            job = connection.execute(
                "SELECT run_id FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            connection.execute(
                """UPDATE jobs SET current_step=?, progress_percent=CASE
                       WHEN ? IS NULL THEN progress_percent
                       WHEN progress_percent < ? THEN ?
                       ELSE progress_percent END
                   WHERE id=?""",
                (step, progress, progress, progress, job_id),
            )
            if job:
                connection.execute(
                    "UPDATE processing_runs SET current_step=? WHERE id=?",
                    (step, job["run_id"]),
                )

    def _update_job_progress(self, job_id: int, progress_percent: int) -> None:
        progress = max(0, min(99, int(progress_percent)))
        with self._connect() as connection:
            connection.execute(
                """UPDATE jobs SET progress_percent=?
                   WHERE id=? AND status IN ('downloading', 'processing')
                     AND progress_percent < ?""",
                (progress, job_id, progress),
            )

    def _start_job_phase(self, job_id: int, label: str) -> None:
        """Start a measurable phase independent of the overall progress."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE jobs SET phase_label=?, phase_progress_percent=NULL
                   WHERE id=?""",
                (label, job_id),
            )

    def _update_job_phase_progress(self, job_id: int, fraction: float) -> None:
        progress = round(max(0.0, min(1.0, fraction)) * 100)
        with self._connect() as connection:
            connection.execute(
                """UPDATE jobs SET phase_progress_percent=?
                   WHERE id=? AND status IN ('downloading', 'processing')""",
                (progress, job_id),
            )

    def _job_progress_callback(
        self,
        job_id: int,
        start_percent: float,
        end_percent: float,
        phase_start: float = 0.0,
        phase_end: float = 1.0,
    ) -> Callable[[float], None]:
        span = max(0.0, end_percent - start_percent)
        phase_span = max(0.0, phase_end - phase_start)

        def report(fraction: float) -> None:
            bounded = max(0.0, min(1.0, fraction))
            self._update_job_progress(job_id, round(start_percent + span * bounded))
            self._update_job_phase_progress(
                job_id, phase_start + phase_span * bounded
            )

        return report

    def _finish_job(self, job: dict, status: str, error: str | None = None) -> None:
        now = utc_now()
        run_status = "success" if status == "completed" else "error"
        with self._connect() as connection:
            current = connection.execute(
                "SELECT current_step FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            failed_step = current["current_step"] if current else None
            step = (
                "Fertig"
                if status == "completed"
                else f"Fehler bei: {failed_step}" if failed_step else "Fehler"
            )
            connection.execute(
                """UPDATE jobs SET status=?, current_step=?, finished_at=?,
                   error_message=?, progress_percent=CASE
                       WHEN ?='completed' THEN 100 ELSE progress_percent END,
                   phase_progress_percent=CASE
                       WHEN ?='completed' THEN 100 ELSE phase_progress_percent END
                   WHERE id=?""",
                (status, step, now, error, status, status, job["id"]),
            )
            connection.execute(
                """UPDATE processing_runs SET status=?, current_step=?, finished_at=?,
                   error_message=?, normalization_status=CASE
                       WHEN normalization_requested=1 AND ?='success' THEN 'normalized'
                       WHEN normalization_requested=1 THEN 'failed'
                       ELSE normalization_status END
                   WHERE id=?""",
                (run_status, step, now, error, run_status, job["run_id"]),
            )
        if status == "completed":
            LOGGER.info("[job %s] completed", job["id"])
        else:
            LOGGER.error("[job %s] failed: %s", job["id"], error)
        self._queue_wakeup.set()

    def _job_work_directory(self, job: dict) -> Path:
        path = self.config.work_root / "jobs" / (
            f"{job['id']}-{safe_path_component(job['asin'] or 'job')}"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup_job_work_directory(self, job: dict) -> None:
        jobs_root = (self.config.work_root / "jobs").resolve()
        path = jobs_root / f"{job['id']}-{safe_path_component(job['asin'] or 'job')}"
        resolved = path.resolve()
        try:
            resolved.relative_to(jobs_root)
        except ValueError:
            LOGGER.error("[job %s] refused unsafe work cleanup: %s", job["id"], path)
            return
        shutil.rmtree(resolved, ignore_errors=True)

    def cleanup_interrupted_work_directories(self) -> int:
        with self._connect() as connection:
            jobs = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM jobs
                       WHERE status='failed' AND current_step='Unterbrochen'"""
                )
            ]
        for job in jobs:
            self._cleanup_job_work_directory(job)
        staging_directories = []
        if self.config.work_root.is_dir():
            staging_directories = [
                path
                for path in self.config.work_root.rglob(".audible-multipart-*")
                if path.is_dir()
            ]
        for path in staging_directories:
            shutil.rmtree(path, ignore_errors=True)
        return len(jobs) + len(staging_directories)

    def recover_waiting_jobs(self) -> int:
        """Fail staged conversions whose downloaded inputs did not survive restart."""
        with self._connect() as connection:
            jobs = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM jobs
                       WHERE status='waiting_for_processing'
                         AND job_type!='normalize_existing'"""
                )
            ]
        failed = 0
        for job in jobs:
            try:
                self._job_downloaded_files(job)
            except ProcessingError as exc:
                self._finish_job(
                    job,
                    "failed",
                    f"Verarbeitung durch Neustart unterbrochen: {exc}",
                )
                self._cleanup_job_work_directory(job)
                failed += 1
        return failed

    def _activation_bytes(self) -> str:
        if self._activation_bytes_cache:
            return self._activation_bytes_cache
        subprocess.run(["audible", "activation-bytes"], check=True)
        for auth_file in sorted(self.config.config_directory.glob("*.json")):
            try:
                with auth_file.open(encoding="utf-8") as file:
                    activation_bytes = json.load(file).get("activation_bytes")
                if activation_bytes:
                    self._activation_bytes_cache = activation_bytes
                    return activation_bytes
            except (OSError, json.JSONDecodeError):
                continue
        raise ProcessingError("Keine Audible-Aktivierungsbytes in /config gefunden")

    def _work_directory(self, asin: str) -> Path:
        path = self.config.work_root / safe_path_component(asin)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _normalize_downloaded_filename(
        self, work_directory: Path, filename: str, requested_asin: str
    ) -> str:
        new_asin = filename.split("_", 1)[0]
        try:
            self.get_book(new_asin)
            known_asin = True
        except KeyError:
            known_asin = False
        if not known_asin and new_asin != requested_asin:
            new_name = filename.replace(new_asin, requested_asin, 1)
            source = work_directory / filename
            target = work_directory / new_name
            shutil.move(source, target)
            if filename.lower().endswith(".aaxc"):
                old_voucher = source.with_suffix(".voucher")
                new_voucher = target.with_suffix(".voucher")
                if old_voucher.exists():
                    shutil.move(old_voucher, new_voucher)
            return new_name
        return filename

    @staticmethod
    def _run_audible_download_with_progress(
        command: list[str], progress_callback: Callable[[float], None]
    ) -> subprocess.CompletedProcess:
        """Run audible-cli and translate transferred bytes into exact progress."""
        wrapper = Path(__file__).with_name("audible_progress.py")
        progress_command = [sys.executable, str(wrapper), *command[1:]]
        process = subprocess.Popen(
            progress_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_lines = []
        if process.stdout is not None:
            for line in process.stdout:
                value = line.strip()
                if value.startswith(AUDIBLE_PROGRESS_PREFIX):
                    transferred, separator, total = value[
                        len(AUDIBLE_PROGRESS_PREFIX) :
                    ].partition("/")
                    if separator:
                        try:
                            total_bytes = int(total)
                            transferred_bytes = int(transferred)
                        except ValueError:
                            pass
                        else:
                            if total_bytes > 0:
                                progress_callback(transferred_bytes / total_bytes)
                                continue
                output_lines.append(line)
        return_code = process.wait()
        output = "".join(output_lines)
        if return_code:
            raise subprocess.CalledProcessError(
                return_code, command, output=output, stderr=output
            )
        return subprocess.CompletedProcess(command, return_code, output, output)

    def _download_audio(
        self,
        asin: str,
        work_directory: Path,
        progress_callback: Callable[[float], None] | None = None,
    ) -> list[Path]:
        self._activation_bytes()
        work_directory.mkdir(parents=True, exist_ok=True)
        if not work_directory.is_dir():
            raise ProcessingError(
                f"Audible-Arbeitsverzeichnis ist nicht verfügbar: {work_directory}"
            )
        command = [
            "audible",
            "-v",
            "error",
            "download",
            "-a",
            asin,
            "--aax-fallback",
            "--timeout",
            "0",
            "-f",
            "asin_ascii",
            "--ignore-podcasts",
            "--jobs",
            "1",
            "--no-progress",
            "--no-confirm",
            "-o",
            str(work_directory),
        ]
        try:
            if progress_callback is None:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                self._run_audible_download_with_progress(command, progress_callback)
        except subprocess.CalledProcessError as exc:
            details = "\n".join(
                output.strip()
                for output in (exc.stderr, exc.stdout)
                if output and output.strip()
            )
            if not details:
                details = "audible-cli hat keine Fehlerdetails ausgegeben."
            raise ProcessingError(
                f"Audible-Download fehlgeschlagen (Exit {exc.returncode}): {details}"
            ) from exc
        filenames = sorted(
            path.name
            for path in work_directory.iterdir()
            if path.suffix.lower() in (".aax", ".aaxc")
        )
        normalized = [
            self._normalize_downloaded_filename(work_directory, filename, asin)
            for filename in filenames
        ]
        return [work_directory / filename for filename in normalized]

    def _ffmpeg_input_options(self, source: Path) -> list[str]:
        if source.suffix.lower() == ".aax":
            return ["-activation_bytes", self._activation_bytes()]
        if source.suffix.lower() == ".aaxc":
            voucher = source.with_suffix(".voucher")
            if not voucher.exists():
                raise ProcessingError(f"Kein passender Voucher für {source.name} gefunden")
            with voucher.open(encoding="utf-8") as voucher_file:
                license_response = json.load(voucher_file)["content_license"][
                    "license_response"
                ]
            return [
                "-audible_key",
                license_response["key"],
                "-audible_iv",
                license_response["iv"],
            ]
        return []

    @staticmethod
    def _command_json(command: list[str]) -> dict:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True
        )
        return json.loads(completed.stdout or "{}")

    def _probe_audio(self, source: Path, input_options: list[str] | None = None) -> dict:
        probe = self._command_json(
            [
                "ffprobe",
                "-v",
                "error",
                *(input_options or []),
                "-show_streams",
                "-show_chapters",
                "-show_format",
                "-of",
                "json",
                str(source),
            ]
        )
        stream = next(
            (
                item
                for item in probe.get("streams", [])
                if item.get("codec_type") == "audio"
            ),
            None,
        )
        if stream is None:
            raise ProcessingError(f"Keine Audiospur in {source.name} gefunden")
        return {
            "stream": stream,
            "streams": probe.get("streams", []),
            "chapters": probe.get("chapters", []),
            "format": probe.get("format", {}),
        }

    @staticmethod
    def _probe_duration(audio_probe: dict) -> float:
        for raw_duration in (
            audio_probe.get("stream", {}).get("duration"),
            audio_probe.get("format", {}).get("duration"),
        ):
            try:
                duration = float(raw_duration)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                return duration
        return 0.0

    @staticmethod
    def _run_ffmpeg_with_progress(
        command: list[str],
        duration: float,
        progress_callback: Callable[[float], None] | None,
    ) -> subprocess.CompletedProcess:
        """Run FFmpeg and report decoded media time as a fraction from zero to one."""
        if progress_callback is None:
            return subprocess.run(command, check=True)
        if duration <= 0:
            completed = subprocess.run(command, check=True)
            progress_callback(1.0)
            return completed

        progress_command = [
            command[0],
            "-progress",
            "pipe:1",
            "-nostats",
            *command[1:],
        ]
        process = subprocess.Popen(
            progress_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_lines = []
        if process.stdout is not None:
            for line in process.stdout:
                output_lines.append(line)
                key, separator, raw_value = line.strip().partition("=")
                if not separator or key not in ("out_time_us", "out_time_ms"):
                    continue
                try:
                    seconds = int(raw_value) / 1_000_000
                except ValueError:
                    continue
                progress_callback(max(0.0, min(1.0, seconds / duration)))
        return_code = process.wait()
        output = "".join(output_lines)
        if return_code:
            raise subprocess.CalledProcessError(
                return_code, progress_command, output=output, stderr=output
            )
        progress_callback(1.0)
        return subprocess.CompletedProcess(progress_command, return_code, output, output)

    def _aac_encoding_options(self, audio_probe: dict) -> list[str]:
        """Keep source layout/rate and encode near its bitrate, capped at 256 kbit/s.

        Audible speech is commonly 64-128 kbit/s AAC. A known source bitrate is
        reused (with a 32 kbit/s floor); otherwise 64 kbit/s per channel, up to
        192 kbit/s, is a conservative fallback that avoids wasteful bitrates.
        """
        stream = audio_probe["stream"]
        channels = int(stream.get("channels") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        raw_bitrate = stream.get("bit_rate") or audio_probe["format"].get("bit_rate")
        try:
            source_bitrate = int(raw_bitrate)
        except (TypeError, ValueError):
            source_bitrate = 0
        if source_bitrate:
            bitrate = min(256_000, max(32_000, source_bitrate))
        else:
            bitrate = min(192_000, 64_000 * max(1, channels))
        options = ["-c:a", "aac", "-b:a", str(bitrate)]
        if sample_rate:
            options.extend(("-ar:a", str(sample_rate)))
        if channels:
            options.extend(("-ac:a", str(channels)))
        return options

    @staticmethod
    def _cover_disposition_options(audio_probe: dict) -> list[str]:
        options = []
        video_index = 0
        for stream in audio_probe.get("streams", []):
            if stream.get("codec_type") != "video":
                continue
            if stream.get("disposition", {}).get("attached_pic"):
                options.extend((f"-disposition:v:{video_index}", "attached_pic"))
            video_index += 1
        return options

    def _loudnorm_targets(self) -> str:
        return (
            f"I={self.config.normalize_target_i:g}:"
            f"TP={self.config.normalize_target_tp:g}:"
            f"LRA={self.config.normalize_target_lra:g}"
        )

    def _measure_loudness(
        self,
        source: Path,
        input_options: list[str] | None = None,
        duration: float = 0,
        progress_callback: Callable[[float], None] | None = None,
    ) -> dict[str, str]:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            *(input_options or []),
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            f"loudnorm={self._loudnorm_targets()}:print_format=json",
            "-f",
            "null",
            "-",
        ]
        if progress_callback is None:
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True
            )
        else:
            completed = self._run_ffmpeg_with_progress(
                command, duration, progress_callback
            )
        stderr = completed.stderr or ""
        for candidate in reversed(re.findall(r"\{[^{}]+\}", stderr, re.DOTALL)):
            try:
                measured = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if "input_i" in measured and "target_offset" in measured:
                return measured
        raise ProcessingError(f"loudnorm lieferte keine Messwerte für {source.name}")

    def _second_pass_filter(self, measured: dict[str, str]) -> str:
        required = {
            "input_i": "measured_I",
            "input_tp": "measured_TP",
            "input_lra": "measured_LRA",
            "input_thresh": "measured_thresh",
            "target_offset": "offset",
        }
        try:
            finite_measurement = all(
                math.isfinite(float(measured[source])) for source in required
            )
        except (KeyError, TypeError, ValueError):
            finite_measurement = False
        if not finite_measurement:
            LOGGER.warning(
                "loudnorm measurement is not finite; using dynamic single-pass mode"
            )
            return f"loudnorm={self._loudnorm_targets()}:print_format=summary"
        values = ":".join(
            f"{target}={measured[source]}" for source, target in required.items()
        )
        return (
            f"loudnorm={self._loudnorm_targets()}:{values}:"
            "linear=true:print_format=summary"
        )

    def _validate_media_output(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise ProcessingError(f"ffmpeg erzeugte keine Ausgabe für {path.name}")
        probe = self._command_json(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(path),
            ]
        )
        if not any(
            stream.get("codec_type") == "audio"
            for stream in probe.get("streams", [])
        ):
            raise ProcessingError(f"Validierung fand keine Audiospur in {path.name}")
        try:
            duration = float(probe.get("format", {}).get("duration", 0))
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            raise ProcessingError(f"Ungültige Dauer der Ausgabe {path.name}")

    @staticmethod
    def _temporary_normalized_path(destination: Path) -> Path:
        return destination.with_name(
            f".{destination.stem}.audible-normalize-{uuid.uuid4().hex}.tmp.m4b"
        )

    @staticmethod
    def _temporary_merged_path(destination: Path) -> Path:
        return destination.with_name(
            f".{destination.stem}.audible-merge-{uuid.uuid4().hex}.tmp.m4b"
        )

    @staticmethod
    def _ffmetadata_escape(value: object) -> str:
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", " / ")
            .replace("\r", "")
            .replace("=", "\\=")
            .replace(";", "\\;")
            .replace("#", "\\#")
        )

    def _multipart_metadata(
        self, probes: list[dict], book: dict
    ) -> tuple[dict[str, str], list[dict], float]:
        """Merge global tags and offset every part's chapters onto one timeline."""
        ignored_tags = {"major_brand", "minor_version", "compatible_brands", "encoder"}
        tags: dict[str, str] = {}
        chapters = []
        offset = 0.0
        for part_number, probe in enumerate(probes, start=1):
            duration = self._probe_duration(probe)
            if duration <= 0:
                raise ProcessingError(
                    f"Die Dauer von Multipart-Teil {part_number} konnte nicht ermittelt werden"
                )
            for key, value in probe.get("format", {}).get("tags", {}).items():
                if key.lower() not in ignored_tags and value not in (None, ""):
                    tags.setdefault(str(key), str(value))

            source_chapters = probe.get("chapters") or []
            if not source_chapters:
                source_chapters = [
                    {
                        "start_time": 0,
                        "end_time": duration,
                        "tags": {"title": f"Teil {part_number:02d}"},
                    }
                ]
            part_end = offset + duration
            for chapter_number, chapter in enumerate(source_chapters, start=1):
                try:
                    relative_start = max(0.0, float(chapter.get("start_time", 0)))
                    relative_end = float(chapter.get("end_time", duration))
                except (TypeError, ValueError):
                    relative_start = 0.0
                    relative_end = duration
                start = min(part_end, offset + relative_start)
                end = min(part_end, offset + max(relative_start, relative_end))
                if end <= start:
                    end = min(part_end, start + 0.001)
                if end <= start:
                    continue
                chapter_tags = {
                    str(key): str(value)
                    for key, value in (chapter.get("tags") or {}).items()
                    if value not in (None, "")
                }
                chapter_tags.setdefault(
                    "title", f"Teil {part_number:02d}, Kapitel {chapter_number}"
                )
                chapters.append({"start": start, "end": end, "tags": chapter_tags})
            offset = part_end

        title = book.get("title") or tags.get("title") or "Audiobook"
        tags["title"] = title
        tags.setdefault("album", title)
        if book.get("authors"):
            tags.setdefault("artist", book["authors"])
            tags.setdefault("album_artist", book["authors"])
        if book.get("subtitle"):
            tags.setdefault("subtitle", book["subtitle"])
        if book.get("series_title"):
            tags.setdefault("series", book["series_title"])
        if book.get("narrators"):
            tags.setdefault("narrator", book["narrators"])
        if book.get("asin"):
            tags.setdefault("asin", book["asin"])
        return tags, chapters, offset

    def _write_multipart_metadata(
        self,
        path: Path,
        tags: dict[str, str],
        chapters: list[dict],
    ) -> None:
        lines = [";FFMETADATA1"]
        for key, value in tags.items():
            lines.append(
                f"{self._ffmetadata_escape(key)}={self._ffmetadata_escape(value)}"
            )
        for chapter in chapters:
            start_milliseconds = round(chapter["start"] * 1000)
            end_milliseconds = max(
                start_milliseconds + 1, round(chapter["end"] * 1000)
            )
            lines.extend(
                (
                    "[CHAPTER]",
                    "TIMEBASE=1/1000",
                    f"START={start_milliseconds}",
                    f"END={end_milliseconds}",
                )
            )
            for key, value in chapter["tags"].items():
                lines.append(
                    f"{self._ffmetadata_escape(key)}={self._ffmetadata_escape(value)}"
                )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _merge_m4b_parts(
        self,
        parts: list[Path],
        destination: Path,
        book: dict,
        job_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        """Stream-copy decrypted parts into one atomically published M4B."""
        if len(parts) < 2:
            raise ProcessingError("Zum Zusammenführen werden mindestens zwei Teile benötigt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        probes = [self._probe_audio(part) for part in parts]
        tags, chapters, expected_duration = self._multipart_metadata(probes, book)
        merge_directory = Path(
            tempfile.mkdtemp(prefix="concat-", dir=parts[0].parent)
        )
        concat_file = merge_directory / "concat.txt"
        metadata_file = merge_directory / "merged.ffmetadata"

        cover_index = next(
            (
                index
                for index, probe in enumerate(probes)
                if any(
                    stream.get("codec_type") == "video"
                    and stream.get("disposition", {}).get("attached_pic")
                    for stream in probe.get("streams", [])
                )
            ),
            0,
        )
        cover_source = parts[cover_index]
        cover_probe = probes[cover_index]
        audio_layouts = {
            (
                probe["stream"].get("codec_name"),
                probe["stream"].get("sample_rate"),
                probe["stream"].get("channels"),
                probe["stream"].get("channel_layout"),
            )
            for probe in probes
        }
        compatible_audio = len(audio_layouts) == 1
        audio_options = (
            ["-c", "copy"]
            if compatible_audio
            else self._aac_encoding_options(probes[0])
        )
        if not compatible_audio:
            LOGGER.warning(
                "%smultipart audio layouts differ; encoding parts to matching AAC",
                f"[job {job_id}] " if job_id is not None else "",
            )
        temporary = self._temporary_merged_path(destination)
        prefix = f"[job {job_id}] " if job_id is not None else ""
        LOGGER.info("%smerging %s audiobook parts", prefix, len(parts))
        try:
            audio_parts = []
            # Extracting all audio and concatenating it each process one complete
            # audiobook duration, so both halves carry the same amount of work.
            remux_end = 0.5
            processed_duration = 0.0
            for index, (part, probe) in enumerate(zip(parts, probes), start=1):
                audio_part = merge_directory / f"audio-{index:04d}.m4a"
                part_duration = self._probe_duration(probe)
                if expected_duration > 0:
                    part_start = remux_end * processed_duration / expected_duration
                    processed_duration += part_duration
                    part_end = remux_end * processed_duration / expected_duration
                else:
                    part_start = remux_end * (index - 1) / len(parts)
                    part_end = remux_end * index / len(parts)
                self._run_ffmpeg_with_progress(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(part),
                        "-map",
                        "0:a:0",
                        "-map_metadata",
                        "-1",
                        "-map_chapters",
                        "-1",
                        *audio_options,
                        str(audio_part),
                    ],
                    self._probe_duration(probe),
                    self._scaled_progress_callback(
                        progress_callback, part_start, part_end
                    ),
                )
                audio_parts.append(audio_part)
            concat_file.write_text(
                "".join(f"file '{part.name}'\n" for part in audio_parts),
                encoding="utf-8",
            )
            self._write_multipart_metadata(metadata_file, tags, chapters)
            command = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-i",
                str(cover_source),
                "-f",
                "ffmetadata",
                "-i",
                str(metadata_file),
                "-map",
                "0:a:0",
                "-map",
                "1:v?",
                "-map",
                "1:s?",
                "-map_metadata",
                "2",
                "-map_metadata:s:a:0",
                "1:s:a:0",
                "-map_chapters",
                "2",
                "-c",
                "copy",
                *self._cover_disposition_options(cover_probe),
                "-movflags",
                "+faststart",
                str(temporary),
            ]
            self._run_ffmpeg_with_progress(
                command,
                expected_duration,
                self._scaled_progress_callback(progress_callback, remux_end, 1.0),
            )
            self._validate_media_output(temporary)
            merged_duration = self._probe_duration(self._probe_audio(temporary))
            tolerance = max(2.0, expected_duration * 0.01)
            if abs(merged_duration - expected_duration) > tolerance:
                raise ProcessingError(
                    "Die Dauer der zusammengeführten M4B weicht von der Summe "
                    "der Teile ab"
                )
            with temporary.open("rb") as output_file:
                os.fsync(output_file.fileno())
            os.replace(temporary, destination)
            LOGGER.info("%smultipart output published: %s", prefix, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            shutil.rmtree(merge_directory, ignore_errors=True)

    def _transcode_loudnorm(
        self,
        source: Path,
        destination: Path,
        input_options: list[str] | None = None,
        job_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
    ) -> dict[str, str]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._temporary_normalized_path(destination)
        prefix = f"[job {job_id}] " if job_id is not None else ""
        LOGGER.info("%sstarting loudness measurement for %s", prefix, source)
        try:
            audio_probe = self._probe_audio(source, input_options)
            duration = self._probe_duration(audio_probe)
            measurement_progress = (
                (lambda fraction: progress_callback(fraction * 0.5))
                if progress_callback
                else None
            )
            if measurement_progress is None:
                measured = self._measure_loudness(source, input_options)
            else:
                measured = self._measure_loudness(
                    source,
                    input_options,
                    duration=duration,
                    progress_callback=measurement_progress,
                )
            command = [
                "ffmpeg",
                "-y",
                *(input_options or []),
                "-i",
                str(source),
                "-map",
                "0:a",
                "-map",
                "0:v?",
                "-map",
                "0:s?",
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-c",
                "copy",
                *self._aac_encoding_options(audio_probe),
                "-af",
                self._second_pass_filter(measured),
                *self._cover_disposition_options(audio_probe),
                str(temporary),
            ]
            LOGGER.info("%sstarting loudness normalization for %s", prefix, source)
            encoding_progress = (
                (lambda fraction: progress_callback(0.5 + fraction * 0.5))
                if progress_callback
                else None
            )
            self._run_ffmpeg_with_progress(command, duration, encoding_progress)
            self._validate_media_output(temporary)
            with temporary.open("rb") as output_file:
                os.fsync(output_file.fileno())
            os.replace(temporary, destination)
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                LOGGER.debug("Could not fsync directory %s", destination.parent)
            LOGGER.info("%soutput replaced successfully: %s", prefix, destination)
            return measured
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def normalize_existing_file(
        self,
        path: str | Path,
        job_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        """Safely normalize an M4B and atomically replace it only after validation."""
        source = self.normalize_target_path(path)
        if not source.is_file() or source.stat().st_size == 0:
            raise ProcessingError(f"Ausgabedatei fehlt: {source}")
        self._transcode_loudnorm(
            source,
            source,
            job_id=job_id,
            progress_callback=progress_callback,
        )

    def cleanup_normalization_temporaries(self) -> int:
        removed = 0
        if not self.config.audiobook_root.is_dir():
            return removed
        for pattern in (
            ".*.audible-normalize-*.tmp.m4b",
            ".*.audible-merge-*.tmp.m4b",
        ):
            for path in self.config.audiobook_root.rglob(pattern):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    LOGGER.warning("Temporäre Mediendatei blieb erhalten: %s", path)
        if removed:
            LOGGER.info("Removed %s interrupted normalization temporary files", removed)
        return removed

    def _convert_file(
        self,
        source: Path,
        destination: Path,
        overwrite: bool = False,
        normalize: bool = False,
        job_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        input_options = self._ffmpeg_input_options(source)
        if normalize:
            self._transcode_loudnorm(
                source,
                destination,
                input_options=input_options,
                job_id=job_id,
                progress_callback=progress_callback,
            )
            source.unlink()
            voucher = source.with_suffix(".voucher")
            if voucher.exists():
                voucher.unlink()
            return
        output_option = "-y" if overwrite else "-n"
        command = [
            "ffmpeg",
            output_option,
            *input_options,
            "-i",
            str(source),
            "-c",
            "copy",
            str(destination),
        ]
        duration = 0.0
        if progress_callback is not None:
            try:
                duration = self._probe_duration(
                    self._probe_audio(source, input_options)
                )
            except Exception:
                LOGGER.warning(
                    "Could not determine duration for progress of %s", source,
                    exc_info=True,
                )
        self._run_ffmpeg_with_progress(command, duration, progress_callback)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProcessingError(f"ffmpeg erzeugte keine Ausgabe für {source.name}")
        source.unlink()
        voucher = source.with_suffix(".voucher")
        if voucher.exists():
            voucher.unlink()

    @staticmethod
    def _scaled_progress_callback(
        progress_callback: Callable[[float], None] | None,
        start: float,
        end: float,
    ) -> Callable[[float], None] | None:
        if progress_callback is None:
            return None
        span = max(0.0, end - start)

        def report(fraction: float) -> None:
            progress_callback(start + span * max(0.0, min(1.0, fraction)))

        return report

    def _convert_downloaded_files(
        self,
        audio_files: list[Path],
        destination: Path,
        book: dict,
        overwrite: bool,
        normalize: bool,
        job_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
        step_callback: Callable[[str], None] | None = None,
    ) -> Path:
        """Convert one download or merge all multipart downloads into one M4B."""
        if not audio_files:
            raise ProcessingError("Keine heruntergeladenen Audiodateien vorhanden")
        if destination.exists() and not overwrite:
            raise TargetExistsError(
                f"Zieldatei bereits vorhanden: {destination}. "
                "Überschreiben muss bestätigt werden."
            )
        if len(audio_files) == 1:
            if step_callback:
                step_callback(
                    "Normalisierung mit ffmpeg" if normalize else "Konvertierung mit ffmpeg"
                )
            extra_options = {}
            if job_id is not None:
                extra_options["job_id"] = job_id
            if progress_callback is not None:
                extra_options["progress_callback"] = progress_callback
            if normalize:
                self._convert_file(
                    audio_files[0],
                    destination,
                    overwrite=overwrite,
                    normalize=True,
                    **extra_options,
                )
            else:
                self._convert_file(
                    audio_files[0],
                    destination,
                    overwrite=overwrite,
                    **extra_options,
                )
            return destination

        staging_directory = Path(
            tempfile.mkdtemp(prefix=".audible-multipart-", dir=audio_files[0].parent)
        )
        prefix = f"[job {job_id}] " if job_id is not None else ""
        # A multipart conversion processes one audiobook duration while the
        # merge processes it twice (audio extraction and final concatenation).
        # Loudness normalization adds another two complete passes.
        conversion_end = 0.2 if normalize else 1 / 3
        merge_end = 0.6 if normalize else 1.0
        part_weights = [1.0] * len(audio_files)
        if progress_callback is not None:
            measured_weights = []
            for source in audio_files:
                try:
                    measured_weights.append(
                        self._probe_duration(
                            self._probe_audio(source, self._ffmpeg_input_options(source))
                        )
                    )
                except Exception:
                    measured_weights = []
                    LOGGER.warning(
                        "%scould not determine all multipart durations for progress",
                        prefix,
                        exc_info=True,
                    )
                    break
            if measured_weights and sum(measured_weights) > 0:
                part_weights = measured_weights
        total_part_weight = sum(part_weights)
        completed_part_weight = 0.0
        converted_parts = []
        try:
            for index, source in enumerate(audio_files, start=1):
                if step_callback:
                    step_callback(
                        f"Multipart-Teil {index}/{len(audio_files)} wird entschlüsselt"
                    )
                part_start = conversion_end * completed_part_weight / total_part_weight
                completed_part_weight += part_weights[index - 1]
                part_end = conversion_end * completed_part_weight / total_part_weight
                converted = staging_directory / f"part-{index:04d}.m4b"
                extra_options = {}
                if job_id is not None:
                    extra_options["job_id"] = job_id
                part_progress = self._scaled_progress_callback(
                    progress_callback, part_start, part_end
                )
                if part_progress is not None:
                    extra_options["progress_callback"] = part_progress
                self._convert_file(
                    source,
                    converted,
                    overwrite=False,
                    **extra_options,
                )
                converted_parts.append(converted)

            if step_callback:
                step_callback("Multipart-Teile und Kapitel werden zusammengeführt")
            merged_destination = (
                staging_directory / "merged.m4b" if normalize else destination
            )
            self._merge_m4b_parts(
                converted_parts,
                merged_destination,
                book,
                job_id=job_id,
                progress_callback=self._scaled_progress_callback(
                    progress_callback, conversion_end, merge_end
                ),
            )
            if normalize:
                if step_callback:
                    step_callback("Zusammengeführtes Hörbuch wird normalisiert")
                self._transcode_loudnorm(
                    merged_destination,
                    destination,
                    job_id=job_id,
                    progress_callback=self._scaled_progress_callback(
                        progress_callback, merge_end, 1.0
                    ),
                )
            LOGGER.info(
                "%sconverted %s multipart files into one M4B",
                prefix,
                len(audio_files),
            )
            return destination
        finally:
            shutil.rmtree(staging_directory, ignore_errors=True)

    def process_book(
        self,
        book: dict | str,
        target_path: str | Path | None = None,
        force: bool = False,
        overwrite: bool = False,
        trigger: str = "automatic",
        normalize: bool = False,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Download and convert exactly one book through the shared pipeline."""
        book_data = self.get_book(book) if isinstance(book, str) else book
        asin = book_data["asin"]
        target_was_supplied = target_path is not None
        default_target = self.default_target_path(book_data)
        target = self.normalize_target_path(target_path or default_target)
        overridden = target != self.normalize_target_path(default_target)

        lock = self._book_lock(asin)
        if not lock.acquire(blocking=False):
            return ProcessResult("running", error="Dieses Hörbuch wird bereits verarbeitet")

        run_id = None
        try:
            status = self.book_status(asin)
            if status.get("active_job"):
                return ProcessResult(
                    "running",
                    error="Für dieses Hörbuch ist bereits ein Job eingereiht oder aktiv.",
                )
            if status["has_success_history"] and not force:
                run_id = self._create_run(
                    asin,
                    target,
                    overridden,
                    force,
                    overwrite,
                    trigger,
                    status="skipped",
                    operation_type="reconvert" if force else "convert",
                    normalize=normalize,
                )
                self._finish_run(
                    run_id,
                    "skipped",
                    "Bereits verarbeitet; für eine erneute Verarbeitung force=True verwenden",
                )
                return ProcessResult(
                    "skipped",
                    run_id,
                    error="Bereits verarbeitet; Force ist erforderlich",
                )

            run_id = self._create_run(
                asin,
                target,
                overridden,
                force,
                overwrite,
                trigger,
                operation_type="reconvert" if force else "convert",
                normalize=normalize,
            )
            existing = self.existing_target_files(target)
            if existing and not overwrite:
                paths = ", ".join(str(path) for path in existing)
                raise TargetExistsError(
                    f"Zieldatei bereits vorhanden: {paths}. Überschreiben muss bestätigt werden."
                )

            def progress(step: str) -> None:
                self._set_run_step(run_id, step)
                if progress_callback:
                    progress_callback(step)

            progress("Download wird vorbereitet")
            work_directory = self._work_directory(asin)
            progress("Download")
            with self._download_lock:
                audio_files = self._download_audio(asin, work_directory)
            if not audio_files:
                raise ProcessingError(f"Audible lieferte keine Audiodatei für {asin}")

            progress("AAX/AAXC wird verarbeitet")
            destination = (
                self.config.audiobook_root / f"{audio_files[0].stem}.m4b"
                if not self.config.use_folders
                and not target_was_supplied
                and len(audio_files) == 1
                else target
            )
            if destination.exists() and not overwrite:
                raise TargetExistsError(
                    f"Zieldatei bereits vorhanden: {destination}. "
                    "Überschreiben muss bestätigt werden."
                )

            with self._media_lock:
                self._convert_downloaded_files(
                    audio_files,
                    destination,
                    book_data,
                    overwrite=overwrite,
                    normalize=normalize,
                    step_callback=progress,
                )
            self._record_output(
                run_id,
                destination,
                None,
                "normalized" if normalize else "not_normalized",
            )

            progress("Metadaten werden geschrieben")
            with self._connect() as connection:
                connection.execute(
                    "UPDATE audiobooks SET downloaded=1 WHERE asin=?", (asin,)
                )
            self._finish_run(run_id, "success")

            for voucher in work_directory.glob("*.voucher"):
                voucher.unlink()
            try:
                work_directory.rmdir()
            except OSError:
                pass
            return ProcessResult(
                "success",
                run_id,
                (self.relative_output_path(destination),),
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if run_id is not None:
                self._finish_run(run_id, "error", message)
            LOGGER.exception("Verarbeitung von %s fehlgeschlagen", asin)
            return ProcessResult("error", run_id, error=message)
        finally:
            lock.release()

    def _destinations_for_job(
        self, job: dict, book: dict, audio_files: list[Path]
    ) -> list[Path]:
        target = self.normalize_target_path(job["requested_target_path"])
        if (
            not self.config.use_folders
            and not job["target_was_supplied"]
            and len(audio_files) == 1
        ):
            return [self.config.audiobook_root / f"{audio_files[0].stem}.m4b"]
        return [target]

    def run_next_download_job(self) -> bool:
        """Claim and execute one durable download stage, if one is waiting."""
        job = self._claim_job("queued", "downloading", "Download wird vorbereitet")
        if not job:
            return False
        LOGGER.info("[job %s] starting Audible download", job["id"])
        self._update_job_step(job["id"], "Download wird vorbereitet", 1)
        work_directory = self._job_work_directory(job)
        try:
            target = self.normalize_target_path(job["requested_target_path"])
            existing = self.existing_target_files(target)
            if existing and not job["overwrite"]:
                paths = ", ".join(str(path) for path in existing)
                raise TargetExistsError(
                    f"Zieldatei bereits vorhanden: {paths}. Überschreiben muss bestätigt werden."
                )
            self._update_job_step(job["id"], "Download", 5)
            self._start_job_phase(job["id"], "Download (aktuelle Datei)")
            with self._download_lock:
                audio_files = self._download_audio(
                    job["asin"],
                    work_directory,
                    progress_callback=lambda fraction: self._update_job_phase_progress(
                        job["id"], fraction
                    ),
                )
            if not audio_files:
                raise ProcessingError(
                    f"Audible lieferte keine Audiodatei für {job['asin']}"
                )
            with self._connect() as connection:
                connection.execute(
                    """UPDATE jobs SET status='waiting_for_processing',
                       current_step='Wartet auf Verarbeitung', downloaded_files=?,
                       progress_percent=30, phase_progress_percent=100
                       WHERE id=? AND status='downloading'""",
                    (json.dumps([str(path) for path in audio_files]), job["id"]),
                )
                connection.execute(
                    """UPDATE processing_runs SET current_step='Wartet auf Verarbeitung'
                       WHERE id=?""",
                    (job["run_id"],),
                )
            LOGGER.info("[job %s] Audible download completed", job["id"])
            self._queue_wakeup.set()
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            LOGGER.exception("[job %s] download failed", job["id"])
            self._finish_job(job, "failed", message)
            self._cleanup_job_work_directory(job)
        return True

    def _job_downloaded_files(self, job: dict) -> list[Path]:
        try:
            raw_paths = json.loads(job["downloaded_files"] or "[]")
        except json.JSONDecodeError as exc:
            raise ProcessingError("Gespeicherte Download-Liste ist ungültig") from exc
        if not isinstance(raw_paths, list) or not all(
            isinstance(raw_path, str) for raw_path in raw_paths
        ):
            raise ProcessingError("Gespeicherte Download-Liste ist ungültig")
        work_root = self.config.work_root.resolve()
        paths = []
        for raw_path in raw_paths:
            path = Path(raw_path).resolve()
            try:
                path.relative_to(work_root)
            except ValueError as exc:
                raise ProcessingError(
                    "Gespeicherter Download-Pfad liegt außerhalb des Arbeitsverzeichnisses"
                ) from exc
            if not path.is_file():
                raise ProcessingError(f"Heruntergeladene Datei fehlt: {path}")
            paths.append(path)
        if not paths:
            raise ProcessingError("Keine heruntergeladenen Audiodateien gespeichert")
        return paths

    def _mark_output_normalized(self, path: Path) -> None:
        relative_path = self.relative_output_path(path)
        with self._connect() as connection:
            connection.execute(
                """UPDATE output_files SET normalization_status='normalized'
                   WHERE relative_path=?""",
                (relative_path,),
            )

    def _process_conversion_job(self, job: dict) -> None:
        book = self.get_book(job["asin"])
        audio_files = self._job_downloaded_files(job)
        destination = self._destinations_for_job(job, book, audio_files)[0]
        if destination.exists() and not job["overwrite"]:
            raise TargetExistsError(
                f"Zieldatei bereits vorhanden: {destination}. "
                "Überschreiben muss bestätigt werden."
            )
        self._convert_downloaded_files(
            audio_files,
            destination,
            book,
            overwrite=bool(job["overwrite"]),
            normalize=bool(job["normalize"]),
            job_id=job["id"],
            progress_callback=self._job_progress_callback(job["id"], 30, 99),
            step_callback=lambda step: self._update_job_step(job["id"], step),
        )
        LOGGER.info("[job %s] ffmpeg completed: %s", job["id"], destination)
        self._record_output(
            job["run_id"],
            destination,
            None,
            "normalized" if job["normalize"] else "not_normalized",
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE audiobooks SET downloaded=1 WHERE asin=?", (job["asin"],)
            )

    def _process_normalization_job(self, job: dict) -> None:
        with self._connect() as connection:
            outputs = self._outputs_for_run(connection, job["run_id"])
        if not outputs:
            raise ProcessingError("Normalisierungsjob enthält keine Ausgabedateien")
        paths = [
            self.normalize_target_path(output["relative_path"]) for output in outputs
        ]
        weights = [1.0] * len(paths)
        if len(paths) > 1:
            try:
                measured_weights = [
                    self._probe_duration(self._probe_audio(path)) for path in paths
                ]
                if all(weight > 0 for weight in measured_weights):
                    weights = measured_weights
            except Exception:
                LOGGER.warning(
                    "[job %s] could not determine all normalization durations for progress",
                    job["id"],
                    exc_info=True,
                )
        total_weight = sum(weights)
        completed_weight = 0.0
        for index, (path, weight) in enumerate(zip(paths, weights), start=1):
            phase_start = completed_weight / total_weight
            completed_weight += weight
            phase_end = completed_weight / total_weight
            part_start = 1 + phase_start * 98
            part_end = 1 + phase_end * 98
            self._update_job_step(
                job["id"],
                f"Normalisierung mit ffmpeg ({index}/{len(outputs)})",
                round(part_start),
            )
            self.normalize_existing_file(
                path,
                job_id=job["id"],
                progress_callback=self._job_progress_callback(
                    job["id"],
                    part_start,
                    part_end,
                    phase_start,
                    phase_end,
                ),
            )
            LOGGER.info("[job %s] ffmpeg completed: %s", job["id"], path)
            self._mark_output_normalized(path)

    def run_next_media_job(self) -> bool:
        """Claim and execute one media stage while holding the global media lock."""
        job = self._claim_job(
            "waiting_for_processing", "processing", "Verarbeitung wird vorbereitet"
        )
        if not job:
            return False
        LOGGER.info("[job %s] starting media processing", job["id"])
        self._update_job_step(
            job["id"],
            "Verarbeitung wird vorbereitet",
            1 if job["job_type"] == "normalize_existing" else 30,
        )
        self._start_job_phase(
            job["id"],
            "FFmpeg-Normalisierung" if job["job_type"] == "normalize_existing"
            else "FFmpeg-Medienverarbeitung",
        )
        try:
            with self._media_lock:
                if job["job_type"] == "normalize_existing":
                    self._process_normalization_job(job)
                else:
                    self._process_conversion_job(job)
            self._finish_job(job, "completed")
            if job["job_type"] != "normalize_existing":
                self._cleanup_job_work_directory(job)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            LOGGER.exception("[job %s] media processing failed", job["id"])
            self._finish_job(job, "failed", message)
            if job["job_type"] != "normalize_existing":
                self._cleanup_job_work_directory(job)
        return True

    def _worker_loop(self, stage: str) -> None:
        runner = (
            self.run_next_download_job
            if stage == "download"
            else self.run_next_media_job
        )
        while not self._worker_stop.is_set():
            if runner():
                continue
            self._queue_wakeup.wait(0.5)
            self._queue_wakeup.clear()

    def start_job_workers(self) -> None:
        with self._worker_guard:
            if any(thread.is_alive() for thread in self._worker_threads):
                return
            self._worker_stop.clear()
            self._worker_threads = [
                threading.Thread(
                    target=self._worker_loop,
                    args=(stage,),
                    name=f"job-{stage}-worker",
                    daemon=True,
                )
                for stage in ("download", "media")
            ]
            for thread in self._worker_threads:
                thread.start()
        self._queue_wakeup.set()

    def stop_job_workers(self, timeout: float = 2.0) -> None:
        self._worker_stop.set()
        self._queue_wakeup.set()
        for thread in self._worker_threads:
            thread.join(timeout=timeout)
        self._worker_threads = []

    def process_pending(self) -> list[EnqueueResult]:
        """Persist all books without successful history in the shared queue."""
        with self._connect() as connection:
            asins = [
                row["asin"]
                for row in connection.execute(
                    """SELECT a.asin FROM audiobooks a
                       WHERE COALESCE(a.downloaded, 0)=0
                         AND NOT EXISTS (
                             SELECT 1 FROM processing_runs r
                             WHERE r.asin=a.asin AND r.status='success'
                         )
                       ORDER BY a.rowid"""
                )
            ]
        return [self.enqueue_conversion(asin, trigger="automatic") for asin in asins]

    @staticmethod
    def _asins_from_filename(filename: str, known_asins: dict[str, str]) -> set[str]:
        found = set()
        upper_name = filename.upper()
        prefix = upper_name.split("_", 1)[0]
        if prefix in known_asins:
            found.add(known_asins[prefix])
        for token in re.findall(
            r"(?<![A-Z0-9])[A-Z0-9]{10}(?![A-Z0-9])", upper_name
        ):
            if token in known_asins:
                found.add(known_asins[token])
        return found

    def reconcile_existing_outputs(self) -> int:
        """Import legacy output locations once, then rely on stored paths."""
        with self._connect() as connection:
            books = [dict(row) for row in connection.execute("SELECT * FROM audiobooks")]
            with_processing_history = {
                row["asin"]
                for row in connection.execute(
                    "SELECT DISTINCT asin FROM processing_runs"
                )
            }
        candidates: dict[str, set[Path]] = {
            book["asin"]: set()
            for book in books
            if book["asin"] not in with_processing_history
        }
        known_asins = {asin.upper(): asin for asin in candidates}

        for book in books:
            asin = book["asin"]
            if asin not in candidates:
                continue
            target = self.default_target_path(book)
            candidates[asin].update(self.existing_target_files(target))

        if self.config.audiobook_root.is_dir():
            for path in self.config.audiobook_root.rglob("*.m4b"):
                for asin in self._asins_from_filename(path.name, known_asins):
                    candidates[asin].add(path)

        imported = 0
        for asin, paths in candidates.items():
            existing = sorted(path for path in paths if path.is_file())
            if not existing:
                continue
            target = self.default_target_path(asin)
            run_id = self._create_run(
                asin, target, False, False, False, "legacy", status="running"
            )
            for index, path in enumerate(existing, start=1):
                self._record_output(run_id, path, index if len(existing) > 1 else None)
            with self._connect() as connection:
                connection.execute(
                    "UPDATE audiobooks SET downloaded=1 WHERE asin=?", (asin,)
                )
            self._finish_run(run_id, "success")
            imported += 1
        return imported
