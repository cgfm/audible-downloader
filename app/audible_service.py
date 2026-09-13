"""Shared Audible library, persistent queue, and conversion service.

Direct compatibility calls and queued web/automatic work share the same download,
destination, decryption, conversion, and output-recording helpers.
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from string import Formatter
from urllib.parse import parse_qs, urlparse

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

DEFAULT_SINGLE_BOOK_PATH_TEMPLATE = "{author}/{title}/{asin} - {title}.m4b"
DEFAULT_SERIES_BOOK_PATH_TEMPLATE = (
    "{author}/{series}/Band {series_index} - {title}/{asin} - {title}.m4b"
)
APPLICATION_SETTING_ENVIRONMENT = {
    "auto_download_from_date": "AUTO_DOWNLOAD_FROM_DATE",
    "audiobook_folders": "AUDIOBOOK_FOLDERS",
    "single_book_path_template": "SINGLE_BOOK_PATH_TEMPLATE",
    "series_book_path_template": "SERIES_BOOK_PATH_TEMPLATE",
    "auto_process_enabled": "AUTO_PROCESS_ENABLED",
    "auto_process_interval": "AUTO_PROCESS_INTERVAL",
    "log_level": "LOG_LEVEL",
}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

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

AUDIBLE_MARKETPLACES = {
    "de": "Deutschland",
    "us": "USA",
    "uk": "Vereinigtes Königreich",
    "fr": "Frankreich",
    "ca": "Kanada",
    "it": "Italien",
    "au": "Australien",
    "in": "Indien",
    "jp": "Japan",
    "es": "Spanien",
    "br": "Brasilien",
}

AUDIBLE_MARKETPLACE_DOMAINS = {
    "de": "de",
    "us": "com",
    "uk": "co.uk",
    "fr": "fr",
    "ca": "ca",
    "it": "it",
    "au": "com.au",
    "in": "in",
    "jp": "co.jp",
    "es": "es",
    "br": "com.br",
}

AUTHENTICATION_FLOW_TIMEOUT = 15 * 60

REDACTED = "[REDACTED]"
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)(activation_bytes|audible_key|audible_iv|authorization_code|"
    r"access_token|refresh_token|adp_token|device_private_key|client_secret)"
    r"(\s*(?:=|:)\s*[\"']?)([^\s,\"'&}\]]+)"
)
SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:openid\.oa2\.authorization_code|authorization_code|code|"
    r"access_token|refresh_token)=)([^&#\s]+)"
)
BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
SENSITIVE_OPTION_PATTERN = re.compile(
    r"(?i)((?:-activation_bytes|-audible_key|-audible_iv)"
    r"(?:['\"]?\s*,\s*['\"]?|\s+))([^\s,'\"\]]+)"
)


def redact_sensitive_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Return a bounded diagnostic string with credentials removed."""
    text = str(value or "")
    for secret in sorted(
        {str(secret) for secret in secrets if secret and len(str(secret)) >= 3},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, REDACTED)
    text = SENSITIVE_FIELD_PATTERN.sub(rf"\1\2{REDACTED}", text)
    text = SENSITIVE_QUERY_PATTERN.sub(rf"\1{REDACTED}", text)
    text = BEARER_PATTERN.sub(rf"\1{REDACTED}", text)
    text = SENSITIVE_OPTION_PATTERN.sub(rf"\1{REDACTED}", text)
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    if len(text) > 4096:
        text = f"{text[:4093]}..."
    return text


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def safe_path_component(value: str, max_bytes: int = 180) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "_", value or "")
    value = value.strip().strip(".")
    value = value or "Audiobook"
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        value = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
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
    use_folders: bool = True
    single_book_path_template: str = DEFAULT_SINGLE_BOOK_PATH_TEMPLATE
    series_book_path_template: str = DEFAULT_SERIES_BOOK_PATH_TEMPLATE
    auto_download_from_date: str = ""
    automatic_processing_enabled: bool = True
    automatic_process_interval: int = 21600
    log_level: str = "INFO"
    normalize_target_i: float = -18.0
    normalize_target_tp: float = -1.5
    normalize_target_lra: float = 11.0
    network_timeout_seconds: int = 1800
    media_timeout_seconds: int = 21600
    probe_timeout_seconds: int = 120
    terminate_grace_seconds: int = 10
    minimum_free_bytes: int = 536870912
    max_queue_length: int = 1000
    max_input_bytes: int = 0
    retry_max_attempts: int = 3
    retry_base_seconds: int = 15
    database_backup_retention: int = 7
    job_history_retention_days: int = 0

    @classmethod
    def from_environment(cls) -> ServiceConfig:
        return cls(
            config_directory=Path(os.getenv("AUDIBLE_CONFIG_DIR", "/config")),
            audiobook_root=Path(os.getenv("AUDIOBOOK_DIRECTORY", "/audiobooks")),
            work_root=Path(os.getenv("AUDIBLE_WORK_DIRECTORY", "/app/.audible-downloads")),
            use_folders=os.getenv("AUDIOBOOK_FOLDERS", "True") == "True",
            single_book_path_template=os.getenv(
                "SINGLE_BOOK_PATH_TEMPLATE", DEFAULT_SINGLE_BOOK_PATH_TEMPLATE
            ),
            series_book_path_template=os.getenv(
                "SERIES_BOOK_PATH_TEMPLATE",
                DEFAULT_SERIES_BOOK_PATH_TEMPLATE,
            ),
            auto_download_from_date=os.getenv("AUTO_DOWNLOAD_FROM_DATE", ""),
            automatic_processing_enabled=(
                os.getenv("AUTO_PROCESS_ENABLED", "True") == "True"
            ),
            automatic_process_interval=max(
                60, int(os.getenv("AUTO_PROCESS_INTERVAL", "21600"))
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            normalize_target_i=float(os.getenv("AUDIO_NORMALIZE_TARGET_I", "-18")),
            normalize_target_tp=float(os.getenv("AUDIO_NORMALIZE_TARGET_TP", "-1.5")),
            normalize_target_lra=float(os.getenv("AUDIO_NORMALIZE_TARGET_LRA", "11")),
            network_timeout_seconds=max(
                30, int(os.getenv("AUDIBLE_NETWORK_TIMEOUT", "1800"))
            ),
            media_timeout_seconds=max(
                60, int(os.getenv("MEDIA_PROCESS_TIMEOUT", "21600"))
            ),
            probe_timeout_seconds=max(
                10, int(os.getenv("MEDIA_PROBE_TIMEOUT", "120"))
            ),
            terminate_grace_seconds=max(
                1, int(os.getenv("PROCESS_TERMINATE_GRACE", "10"))
            ),
            minimum_free_bytes=max(
                0, int(os.getenv("MINIMUM_FREE_BYTES", "536870912"))
            ),
            max_queue_length=max(1, int(os.getenv("MAX_QUEUE_LENGTH", "1000"))),
            max_input_bytes=max(0, int(os.getenv("MAX_INPUT_BYTES", "0"))),
            retry_max_attempts=max(
                1, int(os.getenv("JOB_RETRY_MAX_ATTEMPTS", "3"))
            ),
            retry_base_seconds=max(
                1, int(os.getenv("JOB_RETRY_BASE_SECONDS", "15"))
            ),
            database_backup_retention=max(
                1, int(os.getenv("DATABASE_BACKUP_RETENTION", "7"))
            ),
            job_history_retention_days=max(
                0, int(os.getenv("JOB_HISTORY_RETENTION_DAYS", "0"))
            ),
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


@dataclass
class AuthenticationFlow:
    flow_id: str
    country_code: str
    with_username: bool
    queue_pending: bool
    created_at: str = field(default_factory=utc_now)
    status: str = "starting"
    login_url: str | None = None
    error: str | None = None
    callback_url: str | None = field(default=None, repr=False)
    login_url_ready: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    callback_ready: threading.Event = field(
        default_factory=threading.Event, repr=False
    )


class ProcessingError(RuntimeError):
    pass


class TargetExistsError(ProcessingError):
    pass


class TargetOwnershipError(ProcessingError):
    pass


class JobCancelled(ProcessingError):
    pass


class ProcessTimedOut(ProcessingError):
    pass


class AuthenticationError(RuntimeError):
    pass


class AuthenticationCancelled(AuthenticationError):
    pass


class ApplicationConfigurationError(ValueError):
    """Raised when an editable application setting is invalid."""


class AudibleService:
    def __init__(self, config: ServiceConfig | None = None):
        config_was_supplied = config is not None
        self.config = config or ServiceConfig.from_environment()
        self._validate_storage_roots()
        self.database_path = self.config.config_directory / "audiobooks.db"
        self.config.config_directory.mkdir(parents=True, exist_ok=True)
        self.config.audiobook_root.mkdir(parents=True, exist_ok=True)
        self.config.work_root.mkdir(parents=True, exist_ok=True)
        self._harden_runtime_permissions()
        self._book_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._download_lock = threading.Lock()
        self._media_lock = threading.Lock()
        self._worker_guard = threading.Lock()
        self._worker_stop = threading.Event()
        self._queue_wakeup = threading.Event()
        self._application_settings_changed = threading.Event()
        self._worker_threads: list[threading.Thread] = []
        self._process_lock = threading.Lock()
        self._active_processes: dict[int, subprocess.Popen] = {}
        self._cancelled_jobs: set[int] = set()
        self._activation_bytes_cache: str | None = None
        self._secret_values: set[str] = set()
        self._secrets_lock = threading.Lock()
        self._authentication_lock = threading.Lock()
        self._authentication_flows: dict[str, AuthenticationFlow] = {}
        self.cleanup_authentication_staging()
        self._register_configured_secrets()
        self._backup_existing_database("startup")
        self.initialize_database()
        self._initialize_application_settings(config_was_supplied)
        self._apply_application_settings()
        self._cancel_ineligible_automatic_jobs()
        self._harden_sensitive_files()
        self._scrub_persisted_errors()
        self.recover_waiting_jobs()
        self.cleanup_interrupted_work_directories()
        self.cleanup_normalization_temporaries()
        if self.config.job_history_retention_days:
            self.prune_history(self.config.job_history_retention_days)

    def _validate_storage_roots(self) -> None:
        roots = {
            "AUDIBLE_CONFIG_DIR": self.config.config_directory.resolve(),
            "AUDIOBOOK_DIRECTORY": self.config.audiobook_root.resolve(),
            "AUDIBLE_WORK_DIRECTORY": self.config.work_root.resolve(),
        }
        for name, root in roots.items():
            if root == Path(root.anchor):
                raise ValueError(f"{name} darf nicht auf das Dateisystem-Root zeigen.")
        items = list(roots.items())
        for index, (name, root) in enumerate(items):
            for other_name, other_root in items[index + 1 :]:
                if root == other_root or root in other_root.parents or other_root in root.parents:
                    raise ValueError(
                        f"{name} und {other_name} dürfen sich nicht überlappen."
                    )

    def _harden_runtime_permissions(self) -> None:
        for directory in (self.config.config_directory, self.config.work_root):
            try:
                directory.chmod(0o700)
            except OSError:
                LOGGER.warning("Could not restrict permissions for %s", directory)

    def _harden_sensitive_files(self) -> None:
        for path in (
            self.database_path,
            self.config.config_directory / "config.toml",
            *self.config.config_directory.glob("*.json"),
        ):
            if not path.is_file():
                continue
            try:
                path.chmod(0o600)
            except OSError:
                LOGGER.warning("Could not restrict permissions for %s", path.name)

    def _backup_existing_database(self, label: str) -> Path | None:
        """Create a consistent SQLite backup before schema or state changes."""
        if not self.database_path.is_file() or self.database_path.stat().st_size == 0:
            return None
        backup_directory = self.config.config_directory / "backups"
        backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup_directory.chmod(0o700)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = backup_directory / f"audiobooks-{label}-{timestamp}.db"
        try:
            with (
                sqlite3.connect(self.database_path, timeout=30) as source,
                sqlite3.connect(destination) as target,
            ):
                source.backup(target)
            destination.chmod(0o600)
            with destination.open("rb") as backup_file:
                os.fsync(backup_file.fileno())
            self._fsync_directory(backup_directory)
        except (OSError, sqlite3.Error) as exc:
            destination.unlink(missing_ok=True)
            raise ProcessingError(
                "Die bestehende Datenbank konnte vor dem Start nicht gesichert werden."
            ) from exc
        backups = sorted(
            backup_directory.glob("audiobooks-*.db"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for expired in backups[self.config.database_backup_retention :]:
            expired.unlink(missing_ok=True)
        return destination

    def create_database_backup(self) -> Path:
        """Create an on-demand database backup in the private config volume."""
        backup = self._backup_existing_database("manual")
        if backup is None:
            raise ProcessingError("Es gibt noch keine Datenbank zum Sichern.")
        return backup

    def _ensure_free_space(self, path: Path, anticipated_bytes: int = 0) -> None:
        required = max(self.config.minimum_free_bytes, anticipated_bytes)
        try:
            free = shutil.disk_usage(path).free
        except OSError as exc:
            raise ProcessingError(
                f"Freier Speicherplatz für {path} konnte nicht geprüft werden"
            ) from exc
        if free < required:
            raise ProcessingError(
                f"Zu wenig freier Speicherplatz: {free} Bytes verfügbar, "
                f"mindestens {required} Bytes erforderlich"
            )

    def _register_process(
        self, job_id: int | None, process: subprocess.Popen
    ) -> None:
        if job_id is None:
            return
        with self._process_lock:
            if job_id in self._cancelled_jobs:
                self._terminate_process(process)
                raise JobCancelled("Job wurde abgebrochen")
            self._active_processes[job_id] = process

    def _unregister_process(self, job_id: int | None, process: subprocess.Popen) -> None:
        if job_id is None:
            return
        with self._process_lock:
            if self._active_processes.get(job_id) is process:
                self._active_processes.pop(job_id, None)

    def _terminate_process(self, process: subprocess.Popen) -> None:
        try:
            if getattr(process, "pid", None):
                os.killpg(process.pid, signal.SIGTERM)
            elif hasattr(process, "terminate"):
                process.terminate()
            process.wait(timeout=self.config.terminate_grace_seconds)
            return
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            if getattr(process, "pid", None):
                os.killpg(process.pid, signal.SIGKILL)
            elif hasattr(process, "kill"):
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    def _job_cancel_requested(self, job_id: int | None) -> bool:
        if job_id is None:
            return False
        with self._process_lock:
            return job_id in self._cancelled_jobs

    def cleanup_authentication_staging(self) -> int:
        """Remove interrupted secret staging and unreferenced managed auth files."""
        removed = 0
        for path in self.config.config_directory.glob(".audible-auth-*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                if not path.exists():
                    removed += 1

        referenced = set()
        try:
            config = self._load_audible_config()
            for profile in config.get("profile", {}).values():
                if isinstance(profile, dict):
                    filename = self._safe_auth_filename(profile.get("auth_file"))
                    if filename:
                        referenced.add(filename)
        except (OSError, ValueError):
            return removed
        for path in self.config.config_directory.glob("audible-auth-*.json"):
            if path.name in referenced:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                LOGGER.warning("Could not remove orphaned auth file %s", path.name)
        return removed

    def _register_secret(self, value: object) -> None:
        if isinstance(value, str) and len(value) >= 3:
            with self._secrets_lock:
                self._secret_values.add(value)

    def _register_secrets_from_mapping(self, value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in {
                    "activation_bytes",
                    "audible_key",
                    "audible_iv",
                    "authorization_code",
                    "access_token",
                    "refresh_token",
                    "adp_token",
                    "device_private_key",
                    "client_secret",
                }:
                    self._register_secret(item)
                else:
                    self._register_secrets_from_mapping(item)
        elif isinstance(value, list):
            for item in value:
                self._register_secrets_from_mapping(item)

    def _register_configured_secrets(self) -> None:
        for auth_file in self.config.config_directory.glob("*.json"):
            try:
                with auth_file.open(encoding="utf-8") as file:
                    self._register_secrets_from_mapping(json.load(file))
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def _redact(self, value: object) -> str:
        with self._secrets_lock:
            secrets = tuple(self._secret_values)
        return redact_sensitive_text(value, secrets)

    def _safe_exception_message(self, exc: BaseException) -> str:
        message = self._redact(str(exc) or exc.__class__.__name__)
        return message or exc.__class__.__name__

    def public_error_message(self, exc: BaseException) -> str:
        """Return an exception message safe for logs, persistence, and HTTP output."""
        return self._safe_exception_message(exc)

    def _subprocess_failure(
        self, operation: str, exc: subprocess.CalledProcessError
    ) -> ProcessingError:
        details = "\n".join(
            self._redact(output.strip())
            for output in (exc.stderr, exc.stdout)
            if output and output.strip()
        )
        message = f"{operation} fehlgeschlagen (Exit {exc.returncode})"
        if details:
            message = f"{message}: {details}"
        return ProcessingError(message)

    def _scrub_persisted_errors(self) -> int:
        """Redact legacy error rows so old secrets cannot reach the web UI."""
        changed = 0
        with self._connect() as connection:
            for table in ("jobs", "processing_runs"):
                # The table identifier comes only from the fixed tuple above.
                rows = connection.execute(
                    f"SELECT id, error_message FROM {table} WHERE error_message IS NOT NULL"  # nosec B608
                ).fetchall()
                for row in rows:
                    redacted = self._redact(row["error_message"])
                    if redacted != row["error_message"]:
                        connection.execute(
                            f"UPDATE {table} SET error_message=? WHERE id=?",  # nosec B608
                            (redacted, row["id"]),
                        )
                        changed += 1
        if changed:
            LOGGER.warning("Redacted sensitive data from %s persisted errors", changed)
        return changed

    def _load_audible_config(self) -> dict:
        config_path = self.config.config_directory / "config.toml"
        if not config_path.is_file():
            return {"title": "Audible Config File", "APP": {}, "profile": {}}

        import tomllib

        with config_path.open("rb") as file:
            data = tomllib.load(file)
        if not isinstance(data, dict):
            raise ValueError("Die Audible-Konfiguration ist ungültig.")
        data.setdefault("title", "Audible Config File")
        data.setdefault("APP", {})
        data.setdefault("profile", {})
        if not isinstance(data["APP"], dict) or not isinstance(data["profile"], dict):
            raise ValueError("Die Audible-Konfiguration ist ungültig.")
        return data

    @staticmethod
    def _safe_auth_filename(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = Path(value)
        if candidate.name != value or candidate.suffix.lower() != ".json":
            return None
        return value

    def audible_authentication_status(self) -> dict:
        """Describe the locally configured Audible authentication."""
        status = {
            "connected": False,
            "state": "missing",
            "label": "Nicht eingerichtet",
            "profile": None,
            "country_code": None,
            "country_label": None,
            "auth_file": None,
            "with_username": False,
            "error": None,
        }
        try:
            config = self._load_audible_config()
            profiles = config["profile"]
            profile_name = config["APP"].get("primary_profile")
            if not profile_name and profiles:
                profile_name = next(iter(profiles))
            profile = profiles.get(profile_name) if profile_name else None
            if not isinstance(profile, dict):
                return status

            country_code = profile.get("country_code")
            auth_filename = self._safe_auth_filename(profile.get("auth_file"))
            status.update(
                profile=profile_name,
                country_code=country_code,
                country_label=AUDIBLE_MARKETPLACES.get(country_code, country_code),
                auth_file=auth_filename,
            )
            if country_code not in AUDIBLE_MARKETPLACES:
                raise ValueError("Der konfigurierte Audible-Marktplatz ist ungültig.")
            if not auth_filename:
                raise ValueError("Der konfigurierte Name der Auth-Datei ist ungültig.")

            auth_path = self.config.config_directory / auth_filename
            if not auth_path.is_file():
                status["error"] = f"Die Auth-Datei {auth_filename} fehlt."
                status["state"] = "invalid"
                status["label"] = "Einrichtung unvollständig"
                return status

            with auth_path.open(encoding="utf-8") as file:
                auth_data = json.load(file)
            if not isinstance(auth_data, dict):
                raise ValueError("Die Auth-Datei enthält kein JSON-Objekt.")
            if "ciphertext" in auth_data:
                raise ValueError("Die Auth-Datei ist verschlüsselt.")
            signing_auth = auth_data.get("adp_token") and auth_data.get(
                "device_private_key"
            )
            bearer_auth = auth_data.get("access_token") and auth_data.get(
                "refresh_token"
            )
            if not (signing_auth or bearer_auth):
                raise ValueError("Die Auth-Datei enthält keine verwendbare Anmeldung.")

            status.update(
                connected=True,
                state="connected",
                label="Verbunden",
                with_username=bool(auth_data.get("with_username")),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            status["state"] = "invalid"
            status["label"] = "Anmeldung ungültig"
            status["error"] = str(exc)
        return status

    def _audible_config_for_authentication(
        self, country_code: str, auth_filename: str | None = None
    ) -> tuple[dict, str, str]:
        config = self._load_audible_config()

        profiles = config.setdefault("profile", {})
        app_config = config.setdefault("APP", {})
        profile_name = app_config.get("primary_profile")
        if not isinstance(profile_name, str) or not profile_name:
            profile_name = next(iter(profiles), "audible")

        existing_profile = profiles.get(profile_name)
        if not isinstance(existing_profile, dict):
            existing_profile = {}
        auth_filename = auth_filename or self._safe_auth_filename(
            existing_profile.get("auth_file")
        )
        auth_filename = auth_filename or "audible.json"
        existing_profile.update(
            auth_file=auth_filename,
            country_code=country_code,
        )
        profiles[profile_name] = existing_profile
        app_config["primary_profile"] = profile_name
        config["title"] = config.get("title") or "Audible Config File"
        return config, profile_name, auth_filename

    @staticmethod
    def _write_audible_config(path: Path, data: dict) -> None:
        import toml

        path.write_text(toml.dumps(data), encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _create_external_authenticator(
        country_code: str,
        with_username: bool,
        login_url_callback: Callable[[str], str],
    ):
        from audible import Authenticator

        return Authenticator.from_login_external(
            locale=country_code,
            with_username=with_username,
            login_url_callback=login_url_callback,
        )

    def _export_audible_library(
        self, config_directory: Path, output_directory: Path
    ) -> Path:
        library_path = output_directory / "library.tsv"
        environment = os.environ.copy()
        environment["AUDIBLE_CONFIG_DIR"] = str(config_directory)
        subprocess.run(
            ["audible", "library", "export", "--output", str(library_path)],
            check=True,
            cwd=output_directory,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=self.config.network_timeout_seconds,
            start_new_session=True,
        )
        if not library_path.is_file():
            raise AuthenticationError("Audible hat keinen Bibliotheksexport erzeugt.")
        return library_path

    @staticmethod
    def _authentication_flow_snapshot(flow: AuthenticationFlow) -> dict:
        return {
            "flow_id": flow.flow_id,
            "country_code": flow.country_code,
            "country_label": AUDIBLE_MARKETPLACES.get(
                flow.country_code, flow.country_code
            ),
            "with_username": flow.with_username,
            "created_at": flow.created_at,
            "status": flow.status,
            "login_url": flow.login_url,
            "error": flow.error,
        }

    def active_authentication_flow(self) -> dict | None:
        with self._authentication_lock:
            terminal_flow_ids = [
                existing.flow_id
                for existing in self._authentication_flows.values()
                if existing.status in ("completed", "failed", "cancelled")
            ]
            for old_flow_id in terminal_flow_ids[:-10]:
                self._authentication_flows.pop(old_flow_id, None)
            for flow in reversed(self._authentication_flows.values()):
                if flow.status in ("starting", "waiting_for_login", "verifying"):
                    return self._authentication_flow_snapshot(flow)
        return None

    def authentication_flow(self, flow_id: str) -> dict | None:
        with self._authentication_lock:
            flow = self._authentication_flows.get(flow_id)
            if flow is None:
                return None
            return self._authentication_flow_snapshot(flow)

    def start_authentication(
        self,
        country_code: str,
        *,
        with_username: bool = False,
        queue_pending: bool = False,
    ) -> dict:
        """Start a browser-based Audible login without accepting credentials."""
        country_code = country_code.strip().lower()
        if country_code not in AUDIBLE_MARKETPLACES:
            raise ValueError("Dieser Audible-Marktplatz wird nicht unterstützt.")
        if with_username and country_code not in ("de", "us", "uk"):
            raise ValueError(
                "Ein altes Audible-Benutzerkonto wird nur für DE, US und UK unterstützt."
            )
        try:
            self._load_audible_config()
        except (OSError, ValueError) as exc:
            raise ValueError(
                "Die vorhandene Audible-Konfiguration kann nicht sicher aktualisiert werden: "
                f"{exc}"
            ) from exc

        with self._authentication_lock:
            for flow in reversed(self._authentication_flows.values()):
                if flow.status in ("starting", "waiting_for_login", "verifying"):
                    return self._authentication_flow_snapshot(flow)
            flow = AuthenticationFlow(
                flow_id=uuid.uuid4().hex,
                country_code=country_code,
                with_username=with_username,
                queue_pending=queue_pending,
            )
            self._authentication_flows[flow.flow_id] = flow

        threading.Thread(
            target=self._run_authentication_flow,
            args=(flow,),
            name=f"audible-auth-{flow.flow_id[:8]}",
            daemon=True,
        ).start()
        flow.login_url_ready.wait(2)
        return self.authentication_flow(flow.flow_id) or {}

    def _run_authentication_flow(self, flow: AuthenticationFlow) -> None:
        def receive_login_url(login_url: str) -> str:
            parsed = urlparse(login_url)
            if not self._is_allowed_marketplace_url(parsed, flow.country_code):
                raise AuthenticationError("Audible lieferte eine ungültige Anmelde-URL.")
            with self._authentication_lock:
                if flow.status == "cancelled":
                    raise AuthenticationCancelled()
                flow.login_url = login_url
                flow.status = "waiting_for_login"
                flow.login_url_ready.set()

            if not flow.callback_ready.wait(AUTHENTICATION_FLOW_TIMEOUT):
                raise AuthenticationError(
                    "Die Anmeldung wurde nicht innerhalb von 15 Minuten abgeschlossen."
                )
            with self._authentication_lock:
                if flow.status == "cancelled":
                    raise AuthenticationCancelled()
                callback_url = flow.callback_url
                flow.callback_url = None
            if not callback_url:
                raise AuthenticationCancelled()
            return callback_url

        try:
            authenticator = self._create_external_authenticator(
                flow.country_code, flow.with_username, receive_login_url
            )
            with tempfile.TemporaryDirectory(
                prefix=".audible-auth-", dir=self.config.config_directory
            ) as temporary:
                staging_directory = Path(temporary)
                staging_directory.chmod(0o700)
                export_directory = staging_directory / "export"
                export_directory.mkdir()
                new_auth_filename = f"audible-auth-{uuid.uuid4().hex}.json"
                config_data, _, auth_filename = self._audible_config_for_authentication(
                    flow.country_code, new_auth_filename
                )
                staged_auth = staging_directory / auth_filename
                staged_config = staging_directory / "config.toml"
                authenticator.to_file(
                    filename=staged_auth,
                    encryption=False,
                    set_default=False,
                )
                staged_auth.chmod(0o600)
                self._write_audible_config(staged_config, config_data)
                library_path = self._export_audible_library(
                    staging_directory, export_directory
                )

                with self._authentication_lock:
                    if flow.status == "cancelled":
                        raise AuthenticationCancelled()

                final_auth = self.config.config_directory / auth_filename
                final_config = self.config.config_directory / "config.toml"
                with staged_auth.open("rb") as auth_file:
                    os.fsync(auth_file.fileno())
                with staged_config.open("rb") as config_file:
                    os.fsync(config_file.fileno())
                os.replace(staged_auth, final_auth)
                self._fsync_directory(self.config.config_directory)
                os.replace(staged_config, final_config)
                self._fsync_directory(self.config.config_directory)
                self._remove_unreferenced_auth_files(config_data)
                self._activation_bytes_cache = None
                self._register_configured_secrets()
                post_install_error = None
                try:
                    self.refresh_library(library_path)
                    self.reconcile_existing_outputs()
                    if flow.queue_pending:
                        self.process_pending()
                except Exception as exc:
                    LOGGER.error(
                        "Audible post-authentication synchronization failed (%s)",
                        exc.__class__.__name__,
                    )
                    post_install_error = (
                        "Audible ist verbunden, aber die lokale Bibliothek konnte "
                        "nicht vollständig aktualisiert werden."
                    )

            with self._authentication_lock:
                flow.status = "completed"
                flow.login_url = None
                flow.error = post_install_error
        except AuthenticationCancelled:
            with self._authentication_lock:
                flow.status = "cancelled"
                flow.login_url = None
                flow.callback_url = None
        except Exception as exc:
            LOGGER.error(
                "Audible authentication failed (%s)", exc.__class__.__name__
            )
            if isinstance(exc, AuthenticationError):
                message = str(exc)
            elif isinstance(exc, subprocess.TimeoutExpired):
                message = "Audible hat beim Prüfen der Anmeldung nicht rechtzeitig geantwortet."
            elif isinstance(exc, subprocess.CalledProcessError):
                message = "Die Anmeldung wurde erstellt, aber Audible konnte die Bibliothek nicht lesen."
            elif isinstance(exc, ImportError):
                message = "Die Audible-Anmeldebibliothek ist im Container nicht verfügbar."
            else:
                message = "Die Audible-Anmeldung konnte nicht abgeschlossen werden."
            with self._authentication_lock:
                if flow.status != "cancelled":
                    flow.status = "failed"
                    flow.error = message
                    flow.login_url = None
                    flow.callback_url = None
        finally:
            flow.login_url_ready.set()

    @staticmethod
    def _is_allowed_marketplace_url(parsed, country_code: str) -> bool:
        domain = AUDIBLE_MARKETPLACE_DOMAINS[country_code]
        allowed_hosts = {
            f"amazon.{domain}",
            f"www.amazon.{domain}",
            f"audible.{domain}",
            f"www.audible.{domain}",
        }
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname in allowed_hosts
            and port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )

    def _remove_unreferenced_auth_files(self, config: dict) -> None:
        referenced = {
            filename
            for profile in config.get("profile", {}).values()
            if isinstance(profile, dict)
            for filename in [self._safe_auth_filename(profile.get("auth_file"))]
            if filename
        }
        for path in self.config.config_directory.glob("audible-auth-*.json"):
            if path.name not in referenced:
                try:
                    path.unlink()
                except OSError:
                    LOGGER.warning("Could not remove previous auth file %s", path.name)

    @staticmethod
    def _validated_authentication_callback_url(
        callback_url: str, country_code: str
    ) -> str:
        callback_url = callback_url.strip()
        if not callback_url or len(callback_url) > 8192:
            raise ValueError("Bitte die vollständige Weiterleitungs-URL einfügen.")
        parsed = urlparse(callback_url)
        if (
            not AudibleService._is_allowed_marketplace_url(parsed, country_code)
            or parsed.path.rstrip("/") != "/ap/maplanding"
        ):
            raise ValueError(
                "Die URL muss von der Amazon-/Audible-Weiterleitungsseite stammen."
            )
        values = parse_qs(parsed.query).get("openid.oa2.authorization_code")
        if not values or not values[0]:
            raise ValueError("Die URL enthält keinen Audible-Autorisierungscode.")
        return callback_url

    def submit_authentication_callback(self, flow_id: str, callback_url: str) -> dict:
        with self._authentication_lock:
            flow = self._authentication_flows.get(flow_id)
            if flow is None:
                raise KeyError("Unbekannter Anmeldevorgang.")
            if flow.status != "waiting_for_login":
                raise ValueError("Dieser Anmeldevorgang wartet nicht auf eine URL.")
            country_code = flow.country_code

        callback_url = self._validated_authentication_callback_url(
            callback_url, country_code
        )
        authorization_code = parse_qs(urlparse(callback_url).query)[
            "openid.oa2.authorization_code"
        ][0]
        self._register_secret(authorization_code)
        with self._authentication_lock:
            if flow.status != "waiting_for_login":
                raise ValueError("Dieser Anmeldevorgang wurde bereits fortgesetzt.")
            flow.callback_url = callback_url
            flow.status = "verifying"
            flow.callback_ready.set()
            return self._authentication_flow_snapshot(flow)

    def cancel_authentication(self, flow_id: str) -> bool:
        with self._authentication_lock:
            flow = self._authentication_flows.get(flow_id)
            if flow is None or flow.status not in (
                "starting",
                "waiting_for_login",
            ):
                return False
            flow.status = "cancelled"
            flow.login_url = None
            flow.callback_url = None
            flow.callback_ready.set()
            flow.login_url_ready.set()
            return True

    def test_authentication(self) -> int:
        """Verify the configured profile without changing the local library."""
        status = self.audible_authentication_status()
        if not status["connected"]:
            raise AuthenticationError(
                status["error"] or "Audible ist noch nicht eingerichtet."
            )
        with tempfile.TemporaryDirectory(
            prefix="auth-test-", dir=self.config.work_root
        ) as temporary:
            output_directory = Path(temporary)
            library_path = self._export_audible_library(
                self.config.config_directory, output_directory
            )
            with library_path.open(newline="", encoding="utf-8-sig") as file:
                return sum(
                    1
                    for row in csv.DictReader(file, delimiter="\t")
                    if row.get("asin")
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "CASEFOLD",
            1,
            lambda value: str(value or "").casefold(),
            deterministic=True,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        for path in (
            self.database_path,
            self.database_path.with_name(self.database_path.name + "-wal"),
            self.database_path.with_name(self.database_path.name + "-shm"),
        ):
            if path.exists():
                try:
                    path.chmod(0o600)
                except OSError:
                    LOGGER.warning("Could not restrict permissions for %s", path.name)
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
            if "last_seen_at" not in columns:
                connection.execute("ALTER TABLE audiobooks ADD COLUMN last_seen_at TEXT")
            if "library_state" not in columns:
                connection.execute(
                    "ALTER TABLE audiobooks ADD COLUMN library_state TEXT NOT NULL DEFAULT 'active'"
                )
            if "removed_at" not in columns:
                connection.execute("ALTER TABLE audiobooks ADD COLUMN removed_at TEXT")
            connection.execute(
                """UPDATE audiobooks
                   SET last_seen_at=COALESCE(last_seen_at, last_synced_at),
                       library_state=COALESCE(NULLIF(library_state, ''), 'active')"""
            )
            for row in connection.execute(
                "SELECT asin, cover_url FROM audiobooks WHERE COALESCE(cover_url, '') != ''"
            ).fetchall():
                trusted_cover = self._trusted_cover_url(row["cover_url"])
                if trusted_cover != row["cover_url"]:
                    connection.execute(
                        "UPDATE audiobooks SET cover_url=? WHERE asin=?",
                        (trusted_cover, row["asin"]),
                    )

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
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_attempt_at TEXT,
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
                CREATE TABLE IF NOT EXISTS target_path_claims (
                    relative_path TEXT PRIMARY KEY,
                    asin TEXT NOT NULL,
                    run_id INTEGER,
                    job_id INTEGER,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (asin) REFERENCES audiobooks(asin),
                    FOREIGN KEY (run_id) REFERENCES processing_runs(id),
                    FOREIGN KEY (job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS target_path_claims_asin
                    ON target_path_claims(asin);
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

            connection.execute(
                """INSERT OR IGNORE INTO target_path_claims (
                       relative_path, asin, run_id, state, updated_at
                   )
                   SELECT o.relative_path, r.asin, MAX(r.id), 'published', ?
                   FROM output_files o
                   JOIN processing_runs r ON r.id=o.run_id
                   WHERE r.status='success'
                   GROUP BY o.relative_path
                   HAVING COUNT(DISTINCT r.asin)=1""",
                (utc_now(),),
            )
            connection.execute(
                """INSERT OR IGNORE INTO target_path_claims (
                       relative_path, asin, run_id, job_id, state, updated_at
                   )
                   SELECT requested_target_path, asin, run_id, id, 'reserved', ?
                   FROM jobs
                   WHERE asin IS NOT NULL AND status IN (?, ?, ?, ?)""",
                (utc_now(), *ACTIVE_JOB_STATUSES),
            )

            job_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            for name, declaration in (
                ("progress_percent", "INTEGER NOT NULL DEFAULT 0"),
                ("phase_label", "TEXT"),
                ("phase_progress_percent", "INTEGER"),
                ("dismissed_at", "TEXT"),
                ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
                ("next_attempt_at", "TEXT"),
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

    def _runtime_value(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def _set_runtime_value(self, key: str, value: object) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                (key, self._redact(value), utc_now()),
            )

    def _application_setting_defaults(
        self, use_supplied_config: bool
    ) -> dict[str, str]:
        if use_supplied_config:
            use_folders = self.config.use_folders
            single_template = self.config.single_book_path_template
            series_template = self.config.series_book_path_template
            enabled = self.config.automatic_processing_enabled
            interval = self.config.automatic_process_interval
            log_level = self.config.log_level
            configured_date = self.config.auto_download_from_date
        else:
            use_folders = True
            single_template = DEFAULT_SINGLE_BOOK_PATH_TEMPLATE
            series_template = DEFAULT_SERIES_BOOK_PATH_TEMPLATE
            enabled = True
            interval = 21600
            log_level = "INFO"
            configured_date = ""
        return {
            "auto_download_from_date": configured_date
            or datetime.now(UTC).date().isoformat(),
            "audiobook_folders": "True" if use_folders else "False",
            "single_book_path_template": single_template,
            "series_book_path_template": series_template,
            "auto_process_enabled": "True" if enabled else "False",
            "auto_process_interval": str(interval),
            "log_level": log_level,
        }

    @staticmethod
    def _normalized_application_setting(key: str, value: object) -> str:
        raw = str(value).strip()
        if key == "auto_download_from_date":
            try:
                return date.fromisoformat(raw).isoformat()
            except ValueError as exc:
                raise ApplicationConfigurationError(
                    "Das automatische Startdatum muss ein gültiges Datum im "
                    "Format JJJJ-MM-TT sein."
                ) from exc
        if key in {"audiobook_folders", "auto_process_enabled"}:
            normalized = raw.casefold()
            if normalized in {"true", "1", "yes", "on"}:
                return "True"
            if normalized in {"false", "0", "no", "off"}:
                return "False"
            raise ApplicationConfigurationError(
                f"{APPLICATION_SETTING_ENVIRONMENT[key]} muss True oder False sein."
            )
        if key in {"single_book_path_template", "series_book_path_template"}:
            if not raw or len(raw) > 4096:
                raise ApplicationConfigurationError(
                    "Pfad-Templates dürfen nicht leer oder länger als 4096 Zeichen sein."
                )
            try:
                parsed = list(Formatter().parse(raw))
            except ValueError as exc:
                raise ApplicationConfigurationError(
                    "Das Pfad-Template enthält ungültige geschweifte Klammern."
                ) from exc
            for _, field_name, format_spec, conversion in parsed:
                if field_name is None:
                    continue
                if (
                    field_name not in TEMPLATE_FIELDS
                    or format_spec
                    or conversion
                ):
                    raise ApplicationConfigurationError(
                        f"Ungültiger Pfad-Platzhalter: {{{field_name}}}."
                    )
            return raw
        if key == "auto_process_interval":
            try:
                interval = int(raw)
            except ValueError as exc:
                raise ApplicationConfigurationError(
                    "Das Automatikintervall muss eine ganze Zahl sein."
                ) from exc
            if interval < 60:
                raise ApplicationConfigurationError(
                    "Das Automatikintervall muss mindestens 60 Sekunden betragen."
                )
            return str(interval)
        if key == "log_level":
            level = raw.upper()
            if level not in LOG_LEVELS:
                raise ApplicationConfigurationError(
                    "LOG_LEVEL muss DEBUG, INFO, WARNING, ERROR oder CRITICAL sein."
                )
            return level
        raise ApplicationConfigurationError(f"Unbekannte Einstellung: {key}")

    def _initialize_application_settings(self, use_supplied_config: bool) -> None:
        defaults = self._application_setting_defaults(use_supplied_config)
        with self._connect() as connection:
            connection.executemany(
                """INSERT OR IGNORE INTO runtime_state (key, value, updated_at)
                   VALUES (?, ?, ?)""",
                [
                    (key, value, utc_now())
                    for key, value in defaults.items()
                ],
            )

    def application_settings(self) -> dict[str, dict[str, object]]:
        """Return effective editable settings and whether ENV owns each value."""
        with self._connect() as connection:
            persisted = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM runtime_state"
                )
                if row["key"] in APPLICATION_SETTING_ENVIRONMENT
            }
        result = {}
        for key, environment_name in APPLICATION_SETTING_ENVIRONMENT.items():
            environment_locked = environment_name in os.environ
            raw_value = (
                os.environ[environment_name]
                if environment_locked
                else persisted[key]
            )
            try:
                value = self._normalized_application_setting(key, raw_value)
            except ApplicationConfigurationError as exc:
                source = environment_name if environment_locked else key
                raise ApplicationConfigurationError(
                    f"Ungültige Anwendungseinstellung {source}: {exc}"
                ) from exc
            result[key] = {
                "value": value,
                "environment_name": environment_name,
                "environment_locked": environment_locked,
            }
        return result

    def _apply_application_settings(self) -> None:
        settings = self.application_settings()
        values = {key: item["value"] for key, item in settings.items()}
        self.config = replace(
            self.config,
            use_folders=values["audiobook_folders"] == "True",
            single_book_path_template=values["single_book_path_template"],
            series_book_path_template=values["series_book_path_template"],
            auto_download_from_date=values["auto_download_from_date"],
            automatic_processing_enabled=values["auto_process_enabled"] == "True",
            automatic_process_interval=int(values["auto_process_interval"]),
            log_level=values["log_level"],
        )
        logging.getLogger().setLevel(self.config.log_level)

    def update_application_settings(
        self, values: dict[str, object]
    ) -> dict[str, dict[str, object]]:
        """Persist unlocked application settings and apply them immediately."""
        unknown = set(values) - set(APPLICATION_SETTING_ENVIRONMENT)
        if unknown:
            raise ApplicationConfigurationError(
                f"Unbekannte Einstellung: {sorted(unknown)[0]}"
            )
        updates = {}
        for key, value in values.items():
            environment_name = APPLICATION_SETTING_ENVIRONMENT[key]
            if environment_name in os.environ:
                continue
            updates[key] = self._normalized_application_setting(key, value)
        if updates:
            with self._connect() as connection:
                connection.executemany(
                    """INSERT INTO runtime_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=excluded.updated_at""",
                    [
                        (key, self._redact(value), utc_now())
                        for key, value in updates.items()
                    ],
                )
            self._apply_application_settings()
            self._cancel_ineligible_automatic_jobs()
            self._application_settings_changed.set()
        return self.application_settings()

    def wait_for_application_settings_change(self, timeout: float) -> bool:
        changed = self._application_settings_changed.wait(timeout)
        if changed:
            self._application_settings_changed.clear()
        return changed

    def _cancel_ineligible_automatic_jobs(self) -> int:
        """Stop still-queued automatic work that is older than the cutoff."""
        cutoff = self.config.auto_download_from_date
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            jobs = connection.execute(
                """SELECT j.id, j.run_id
                   FROM jobs j
                   JOIN processing_runs r ON r.id=j.run_id
                   JOIN audiobooks a ON a.asin=j.asin
                   WHERE j.status='queued' AND r.trigger='automatic'
                     AND (
                         date(NULLIF(a.purchase_date, '')) IS NULL
                         OR date(a.purchase_date) < date(?)
                     )""",
                (cutoff,),
            ).fetchall()
            for job in jobs:
                detail = "Automatisch übersprungen: Kaufdatum liegt vor der Grenze."
                connection.execute(
                    """UPDATE jobs
                       SET status='cancelled', current_step=?, finished_at=?
                       WHERE id=?""",
                    (detail, now, job["id"]),
                )
                connection.execute(
                    """UPDATE processing_runs
                       SET status='cancelled', current_step=?, finished_at=?
                       WHERE id=?""",
                    (detail, now, job["run_id"]),
                )
                connection.execute(
                    "DELETE FROM target_path_claims WHERE job_id=? AND state='reserved'",
                    (job["id"],),
                )
        if jobs:
            LOGGER.info(
                "%s alte automatische Queue-Einträge durch Kaufdatum-Grenze abgebrochen",
                len(jobs),
            )
        return len(jobs)

    def automatic_processing_paused(self) -> bool:
        return self._runtime_value("automatic_processing_paused", "0") == "1"

    def set_automatic_processing_paused(self, paused: bool) -> None:
        self._set_runtime_value("automatic_processing_paused", int(paused))

    def record_automatic_cycle(
        self, status: str, *, started_at: str, detail: str = ""
    ) -> None:
        self._set_runtime_value("automatic_last_started_at", started_at)
        self._set_runtime_value("automatic_last_finished_at", utc_now())
        self._set_runtime_value("automatic_last_status", status)
        self._set_runtime_value("automatic_last_detail", detail)

    def operational_status(self) -> dict[str, object]:
        keys = (
            "automatic_last_started_at",
            "automatic_last_finished_at",
            "automatic_last_status",
            "automatic_last_detail",
        )
        with self._connect() as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM runtime_state"
                )
                if row["key"] in keys
            }
        return {
            "automatic_paused": self.automatic_processing_paused(),
            "last_started_at": values.get("automatic_last_started_at"),
            "last_finished_at": values.get("automatic_last_finished_at"),
            "last_status": values.get("automatic_last_status"),
            "last_detail": self._redact(values.get("automatic_last_detail", "")),
        }

    def operational_metrics(self) -> dict[str, object]:
        with self._connect() as connection:
            books = {
                row["library_state"]: row["count"]
                for row in connection.execute(
                    "SELECT library_state, COUNT(*) AS count FROM audiobooks GROUP BY library_state"
                )
            }
            jobs = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                )
            }
        return {
            "books": books,
            "jobs": jobs,
            "automatic": self.operational_status(),
        }

    def prune_history(self, retention_days: int) -> int:
        """Remove old dismissed jobs while preserving global path ownership."""
        if retention_days <= 0:
            return 0
        cutoff = (
            datetime.now(UTC) - timedelta(days=retention_days)
        ).isoformat(timespec="seconds")
        removed = 0
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, run_id FROM jobs
                   WHERE dismissed_at IS NOT NULL AND finished_at < ?
                     AND status IN ('completed', 'failed', 'cancelled')""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE target_path_claims SET job_id=NULL WHERE job_id=?",
                    (row["id"],),
                )
                if row["run_id"]:
                    connection.execute(
                        "UPDATE target_path_claims SET run_id=NULL WHERE run_id=?",
                        (row["run_id"],),
                    )
                connection.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                if row["run_id"]:
                    connection.execute(
                        "DELETE FROM processing_runs WHERE id=?", (row["run_id"],)
                    )
                removed += 1
        return removed

    @staticmethod
    def _trusted_cover_url(value: str) -> str:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold()
        try:
            port = parsed.port
        except ValueError:
            return ""
        trusted = hostname == "media-amazon.com" or hostname.endswith(
            (".media-amazon.com", ".ssl-images-amazon.com")
        )
        if parsed.scheme != "https" or port not in (None, 443) or not trusted:
            return ""
        return value

    @classmethod
    def _cover_url(cls, row: dict[str, str]) -> str:
        for key in ("cover_url", "image_url", "picture", "cover"):
            if row.get(key):
                trusted = cls._trusted_cover_url(row[key])
                if trusted:
                    return trusted
        product_images = row.get("product_images", "")
        if product_images:
            try:
                images = json.loads(product_images)
                if isinstance(images, dict):
                    for key in ("500", "300", "200", "1215"):
                        if images.get(key):
                            trusted = cls._trusted_cover_url(images[key])
                            if trusted:
                                return trusted
            except json.JSONDecodeError:
                pass
        return ""

    def sync_library_rows(
        self, rows: Iterable[dict[str, str]], *, complete_snapshot: bool = False
    ) -> dict[str, int]:
        """Insert new Audible entries and update metadata without touching history."""
        added = 0
        updated = 0
        synced_at = datetime.now(UTC).isoformat(timespec="microseconds")
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
                           last_synced_at, purchase_date, last_seen_at, library_state,
                           removed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'active', NULL)
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
                           last_seen_at=excluded.last_seen_at,
                           library_state='active',
                           removed_at=NULL,
                           purchase_date=CASE WHEN excluded.purchase_date != ''
                                              THEN excluded.purchase_date
                                              ELSE audiobooks.purchase_date END""",
                    (*values, synced_at),
                )
                if exists:
                    updated += 1
                else:
                    added += 1
            if complete_snapshot:
                connection.execute(
                    """UPDATE audiobooks
                       SET library_state='removed', removed_at=?
                       WHERE library_state='active'
                         AND COALESCE(last_seen_at, '') != ?""",
                    (utc_now(), synced_at),
                )
        return {"added": added, "updated": updated}

    def refresh_library(self, library_path: Path | None = None) -> dict[str, int]:
        """Fetch and synchronize the library; never starts book processing."""
        temporary_directory = None
        if library_path is None:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="library-", dir=self.config.work_root
            )
            export_directory = Path(temporary_directory.name)
            try:
                subprocess.run(
                    ["audible", "library", "export"],
                    check=True,
                    cwd=export_directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.config.network_timeout_seconds,
                    start_new_session=True,
                )
            except subprocess.CalledProcessError as exc:
                raise self._subprocess_failure(
                    "Audible-Bibliotheksexport", exc
                ) from None
            except subprocess.TimeoutExpired:
                raise ProcessTimedOut(
                    "Audible-Bibliotheksexport überschritt das konfigurierte Zeitlimit"
                ) from None
            library_path = export_directory / "library.tsv"

        try:
            with Path(library_path).open(newline="", encoding="utf-8-sig") as file:
                result = self.sync_library_rows(
                    csv.DictReader(file, delimiter="\t"), complete_snapshot=True
                )
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
        relative = resolved.relative_to(root)
        if len(relative.as_posix().encode("utf-8")) > 3000:
            raise ValueError("Der Zielpfad ist zu lang")
        if any(len(component.encode("utf-8")) > 240 for component in relative.parts):
            raise ValueError("Eine Zielpfad-Komponente ist zu lang")
        return resolved

    def relative_output_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.config.audiobook_root.resolve()).as_posix()

    def _target_ownership_conflict(
        self, connection: sqlite3.Connection, relative_path: str, asin: str
    ) -> str | None:
        claim = connection.execute(
            """SELECT c.asin, a.title FROM target_path_claims c
               LEFT JOIN audiobooks a ON a.asin=c.asin
               WHERE c.relative_path=? AND c.asin!=?""",
            (relative_path, asin),
        ).fetchone()
        if claim:
            return claim["title"] or claim["asin"]
        historical = connection.execute(
            """SELECT r.asin, a.title
               FROM output_files o
               JOIN processing_runs r ON r.id=o.run_id
               LEFT JOIN audiobooks a ON a.asin=r.asin
               WHERE o.relative_path=? AND r.status='success' AND r.asin!=?
               ORDER BY r.id DESC LIMIT 1""",
            (relative_path, asin),
        ).fetchone()
        if historical:
            return historical["title"] or historical["asin"]
        return None

    def target_path_conflict(self, path: Path, asin: str) -> str | None:
        relative_path = self.relative_output_path(self.normalize_target_path(path))
        with self._connect() as connection:
            return self._target_ownership_conflict(connection, relative_path, asin)

    def _claim_target_path(
        self,
        connection: sqlite3.Connection,
        path: Path,
        asin: str,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        state: str = "reserved",
    ) -> None:
        relative_path = self.relative_output_path(self.normalize_target_path(path))
        owner = self._target_ownership_conflict(connection, relative_path, asin)
        if owner:
            raise TargetOwnershipError(
                f"Zielpfad {relative_path} gehört bereits zu {owner}; "
                "Overwrite kann diese Zuordnung nicht ändern."
            )
        connection.execute(
            """INSERT INTO target_path_claims (
                   relative_path, asin, run_id, job_id, state, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(relative_path) DO UPDATE SET
                   run_id=COALESCE(excluded.run_id, target_path_claims.run_id),
                   job_id=COALESCE(excluded.job_id, target_path_claims.job_id),
                   state=CASE
                       WHEN target_path_claims.state='published' THEN 'published'
                       ELSE excluded.state END,
                   updated_at=excluded.updated_at
               WHERE target_path_claims.asin=excluded.asin""",
            (relative_path, asin, run_id, job_id, state, utc_now()),
        )

    def _reserve_target_path(
        self,
        path: Path,
        asin: str,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._claim_target_path(
                connection, path, asin, run_id=run_id, job_id=job_id
            )

    def _collision_free_automatic_target(self, target: Path, asin: str) -> Path:
        if not self.target_path_conflict(target, asin):
            return target
        candidate = target.with_name(f"{target.stem} [{safe_path_component(asin)}]{target.suffix}")
        counter = 2
        while self.target_path_conflict(candidate, asin):
            candidate = target.with_name(
                f"{target.stem} [{safe_path_component(asin)}-{counter}]{target.suffix}"
            )
            counter += 1
        return candidate

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
            return ProcessResult("error", error=self._safe_exception_message(exc))

        if not normalized_paths:
            return ProcessResult("error", error="Mindestens eine M4B-Datei auswählen.")

        normalized_paths.sort(key=lambda path: self.relative_output_path(path).casefold())
        now = utc_now()
        relative_paths = [self.relative_output_path(path) for path in normalized_paths]
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in relative_paths)
                # Only generated question-mark placeholders enter this query.
                association_rows = connection.execute(
                    f"""SELECT o.relative_path, o.normalization_status,
                               r.asin, a.title
                        FROM output_files o
                        JOIN processing_runs r ON r.id=o.run_id
                        LEFT JOIN audiobooks a ON a.asin=r.asin
                        WHERE r.status='success'
                          AND o.relative_path IN ({placeholders})
                        ORDER BY r.id DESC""",  # nosec B608
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
                    self._claim_target_path(
                        connection,
                        self.normalize_target_path(relative_path),
                        asin,
                        run_id=run_id,
                        state="published",
                    )
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
            return ProcessResult("error", error=self._safe_exception_message(exc))

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

    def _compose_book_status(
        self,
        book: dict,
        latest: dict | sqlite3.Row | None,
        latest_success: dict | sqlite3.Row | None,
        active_job: dict | sqlite3.Row | None,
        outputs: list[dict],
    ) -> dict:
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
            "error_message": self._redact(latest["error_message"])
            if latest and latest["status"] == "error" and latest["error_message"]
            else None,
            "has_success_history": bool(latest_success or book.get("downloaded")),
            "outputs": output_state,
            "all_outputs_exist": bool(output_state)
            and all(item["exists"] for item in output_state),
            "normalization": self._normalization_summary(output_state),
            "active_job": self._sanitized_row(active_job),
            "latest_run": self._sanitized_row(latest),
        }

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
        return self._compose_book_status(
            book, latest, latest_success, active_job, outputs
        )

    def _sanitized_row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        if result.get("error_message"):
            result["error_message"] = self._redact(result["error_message"])
        return result

    def list_books(
        self,
        query: str = "",
        status_filter: str = "",
        sort_key: str = "purchase_date",
        sort_direction: str = "desc",
        library_filter: str = "active",
    ) -> list[dict]:
        with self._connect() as connection:
            clauses = []
            parameters: list[object] = []
            if library_filter in ("active", "removed"):
                clauses.append("library_state=?")
                parameters.append(library_filter)
            words = query.casefold().split()
            searchable = (
                "CASEFOLD(COALESCE(title, '') || ' ' || COALESCE(subtitle, '') || ' ' || "
                "COALESCE(authors, '') || ' ' || COALESCE(series_title, '') || ' ' || asin)"
            )
            for word in words:
                clauses.append(f"{searchable} LIKE ?")
                parameters.append(f"%{word}%")
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            # Clauses are selected internally; all user values stay parameterized.
            rows = connection.execute(
                f"SELECT * FROM audiobooks{where} ORDER BY title COLLATE NOCASE, asin",  # nosec B608
                parameters,
            ).fetchall()
            latest_rows = connection.execute(
                """SELECT * FROM (
                       SELECT processing_runs.*,
                              ROW_NUMBER() OVER (PARTITION BY asin ORDER BY id DESC) AS rank
                       FROM processing_runs
                   ) WHERE rank=1"""
            ).fetchall()
            success_rows = connection.execute(
                """SELECT * FROM (
                       SELECT processing_runs.*,
                              ROW_NUMBER() OVER (PARTITION BY asin ORDER BY id DESC) AS rank
                       FROM processing_runs WHERE status='success'
                   ) WHERE rank=1"""
            ).fetchall()
            active_rows = connection.execute(
                """SELECT * FROM jobs
                   WHERE status IN (?, ?, ?, ?)
                   ORDER BY id""",
                ACTIVE_JOB_STATUSES,
            ).fetchall()
            output_rows = connection.execute(
                """SELECT output_files.*, latest.asin
                   FROM output_files
                   JOIN (
                       SELECT asin, MAX(id) AS run_id FROM processing_runs
                       WHERE status='success' GROUP BY asin
                   ) AS latest ON latest.run_id=output_files.run_id
                   ORDER BY COALESCE(output_files.part_number, 0), output_files.id"""
            ).fetchall()
        if sort_key not in SORT_FIELDS:
            sort_key = "purchase_date"
        descending = sort_direction == "desc"
        latest_by_asin = {row["asin"]: row for row in latest_rows}
        success_by_asin = {row["asin"]: row for row in success_rows}
        active_by_asin = {}
        for row in active_rows:
            active_by_asin.setdefault(row["asin"], row)
        outputs_by_asin: dict[str, list[dict]] = {}
        for row in output_rows:
            output = dict(row)
            asin = output.pop("asin")
            outputs_by_asin.setdefault(asin, []).append(output)
        books = []
        for row in rows:
            book = dict(row)
            asin = book["asin"]
            book["status"] = self._compose_book_status(
                book,
                latest_by_asin.get(asin),
                success_by_asin.get(asin),
                active_by_asin.get(asin),
                outputs_by_asin.get(asin, []),
            )
            if status_filter and book["status"]["key"] != status_filter:
                continue
            books.append(book)

        def sort_value(book: dict) -> str:
            value = (
                book["status"]["label"]
                if sort_key == "status"
                else book.get(sort_key)
            )
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
        error = self._redact(error) if error else None
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
            if status not in ("success", "skipped"):
                connection.execute(
                    "DELETE FROM target_path_claims WHERE run_id=? AND state='reserved'",
                    (run_id,),
                )

    def _record_output(
        self,
        run_id: int,
        path: Path,
        part_number: int | None,
        normalization_status: str | None = None,
    ) -> None:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT asin, job_id FROM processing_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ProcessingError("Unbekannter Verarbeitungslauf")
            self._claim_target_path(
                connection,
                path,
                run["asin"],
                run_id=run_id,
                job_id=run["job_id"],
                state="published",
            )
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
            queue_size = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?, ?, ?)",
                ACTIVE_JOB_STATUSES,
            ).fetchone()[0]
            if queue_size >= self.config.max_queue_length:
                return EnqueueResult(
                    0,
                    False,
                    "failed",
                    f"Die maximale Queue-Länge von {self.config.max_queue_length} ist erreicht.",
                )
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

            owner = self._target_ownership_conflict(
                connection, relative_target, asin
            )
            if owner:
                return EnqueueResult(
                    0,
                    False,
                    "failed",
                    f"Zielpfad {relative_target} gehört bereits zu {owner}; "
                    "Overwrite kann diese Zuordnung nicht ändern.",
                )

            cursor = connection.execute(
                """INSERT INTO jobs (
                       job_type, asin, title, created_at, status, current_step,
                       force, overwrite, normalize, requested_target_path,
                       target_overridden, target_was_supplied, dedupe_key,
                       max_attempts
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    self.config.retry_max_attempts,
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
            self._claim_target_path(
                connection,
                target,
                asin,
                run_id=run_id,
                job_id=job_id,
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
        if target_path is None:
            target = self._collision_free_automatic_target(
                target, book_data["asin"]
            )
        else:
            owner = self.target_path_conflict(target, book_data["asin"])
            if owner:
                return EnqueueResult(
                    0,
                    False,
                    "failed",
                    f"Zielpfad {self.relative_output_path(target)} gehört bereits "
                    f"zu {owner}; Overwrite kann diese Zuordnung nicht ändern.",
                )
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
            if job.get("error_message"):
                job["error_message"] = self._redact(job["error_message"])
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
        active_process = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job or job["status"] not in ACTIVE_JOB_STATUSES:
                return False
            if job["status"] in ("downloading", "processing"):
                connection.execute(
                    "UPDATE jobs SET cancel_requested=1, current_step='Abbruch angefordert' WHERE id=?",
                    (job_id,),
                )
                connection.execute(
                    "UPDATE processing_runs SET current_step='Abbruch angefordert' WHERE id=?",
                    (job["run_id"],),
                )
                with self._process_lock:
                    self._cancelled_jobs.add(job_id)
                    active_process = self._active_processes.get(job_id)
            else:
                now = utc_now()
                connection.execute(
                    """UPDATE jobs SET status='cancelled', current_step='Abgebrochen',
                       finished_at=?, cancel_requested=1 WHERE id=?""",
                    (now, job_id),
                )
                connection.execute(
                    """UPDATE processing_runs
                       SET status='cancelled', current_step='Abgebrochen', finished_at=?
                       WHERE id=?""",
                    (now, job["run_id"]),
                )
                connection.execute(
                    "DELETE FROM target_path_claims WHERE job_id=? AND state='reserved'",
                    (job_id,),
                )
        if active_process is not None:
            self._terminate_process(active_process)
        LOGGER.info("[job %s] cancellation requested", job_id)
        self._queue_wakeup.set()
        return True

    @staticmethod
    def _is_temporary_error(exc: BaseException) -> bool:
        if isinstance(exc, (ProcessTimedOut, TimeoutError, ConnectionError)):
            return True
        message = str(exc).casefold()
        return any(
            marker in message
            for marker in (
                "timeout",
                "zeitlimit",
                "temporär",
                "temporary",
                "connection reset",
                "connection refused",
                "network is unreachable",
                "nicht erreichbar",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
            )
        )

    def _schedule_retry(self, job: dict, exc: BaseException, status: str) -> bool:
        if not self._is_temporary_error(exc):
            return False
        attempt_count = int(job.get("attempt_count") or 0) + 1
        max_attempts = int(job.get("max_attempts") or self.config.retry_max_attempts)
        if attempt_count >= max_attempts:
            return False
        delay = self.config.retry_base_seconds * (2 ** (attempt_count - 1))
        next_attempt = (
            datetime.now(UTC) + timedelta(seconds=delay)
        ).isoformat(timespec="seconds")
        message = self._safe_exception_message(exc)
        step = f"Wiederholung in {delay} Sekunden"
        with self._connect() as connection:
            connection.execute(
                """UPDATE jobs SET status=?, current_step=?, error_message=?,
                   attempt_count=?, next_attempt_at=?, cancel_requested=0
                   WHERE id=?""",
                (status, step, message, attempt_count, next_attempt, job["id"]),
            )
            connection.execute(
                """UPDATE processing_runs SET status='queued', current_step=?,
                   error_message=? WHERE id=?""",
                (step, message, job["run_id"]),
            )
        LOGGER.warning(
            "[job %s] temporary failure; retry %s/%s in %s seconds: %s",
            job["id"],
            attempt_count + 1,
            max_attempts,
            delay,
            message,
        )
        self._queue_wakeup.set()
        return True

    def retry_job(self, job_id: int) -> EnqueueResult:
        """Create a fresh job with the exact options of a terminal job."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] not in ("failed", "cancelled"):
                return EnqueueResult(
                    0,
                    False,
                    "failed",
                    "Nur fehlgeschlagene oder abgebrochene Jobs können wiederholt werden.",
                )
            job = dict(row)
            outputs = self._outputs_for_run(connection, job["run_id"])
        book = self.get_book(job["asin"])
        output_paths = [
            (
                self.normalize_target_path(output["relative_path"]),
                output["part_number"],
                output.get("normalization_status"),
            )
            for output in outputs
        ]
        return self._enqueue_job(
            book=book,
            job_type=job["job_type"],
            target=self.normalize_target_path(job["requested_target_path"]),
            target_overridden=bool(job["target_overridden"]),
            target_was_supplied=bool(job["target_was_supplied"]),
            force=bool(job["force"]),
            overwrite=bool(job["overwrite"]),
            normalize=bool(job["normalize"]),
            trigger="manual_retry",
            initial_status=(
                "waiting_for_processing"
                if job["job_type"] == "normalize_existing"
                else "queued"
            ),
            output_paths=(
                output_paths if job["job_type"] == "normalize_existing" else None
            ),
        )

    def _claim_job(self, from_status: str, to_status: str, step: str) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """SELECT * FROM jobs
                   WHERE status=? AND cancel_requested=0
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   ORDER BY id LIMIT 1""",
                (from_status, utc_now()),
            ).fetchone()
            if not job:
                return None
            now = utc_now()
            changed = connection.execute(
                """UPDATE jobs SET status=?, current_step=?, next_attempt_at=NULL,
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
        error = self._redact(error) if error else None
        now = utc_now()
        run_status = (
            "success" if status == "completed" else "cancelled" if status == "cancelled" else "error"
        )
        with self._connect() as connection:
            current = connection.execute(
                "SELECT current_step FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            failed_step = current["current_step"] if current else None
            step = (
                "Fertig"
                if status == "completed"
                else "Abgebrochen" if status == "cancelled"
                else f"Fehler bei: {failed_step}" if failed_step else "Fehler"
            )
            connection.execute(
                """UPDATE jobs SET status=?, current_step=?, finished_at=?,
                   error_message=?, next_attempt_at=NULL, progress_percent=CASE
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
            if status != "completed":
                connection.execute(
                    "DELETE FROM target_path_claims WHERE job_id=? AND state='reserved'",
                    (job["id"],),
                )
        with self._process_lock:
            self._cancelled_jobs.discard(job["id"])
        if status == "completed":
            LOGGER.info("[job %s] completed", job["id"])
        elif status == "cancelled":
            LOGGER.info("[job %s] cancelled", job["id"])
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
        try:
            subprocess.run(
                ["audible", "activation-bytes"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.config.probe_timeout_seconds,
                start_new_session=True,
            )
        except subprocess.CalledProcessError as exc:
            raise self._subprocess_failure(
                "Audible-Aktivierungsbyte-Ermittlung", exc
            ) from None
        except subprocess.TimeoutExpired:
            raise ProcessTimedOut(
                "Audible-Aktivierungsbyte-Ermittlung überschritt das konfigurierte Zeitlimit"
            ) from None
        auth_files = []
        configured = self.audible_authentication_status().get("auth_file")
        if configured:
            auth_files.append(self.config.config_directory / configured)
        auth_files.extend(
            path
            for path in sorted(self.config.config_directory.glob("*.json"))
            if path not in auth_files
        )
        for auth_file in auth_files:
            try:
                with auth_file.open(encoding="utf-8") as file:
                    activation_bytes = json.load(file).get("activation_bytes")
                if activation_bytes:
                    self._register_secret(activation_bytes)
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
            if Path(filename).suffix.lower() in (".aaxc", ".mp3"):
                old_voucher = source.with_suffix(".voucher")
                new_voucher = target.with_suffix(".voucher")
                if old_voucher.exists():
                    shutil.move(old_voucher, new_voucher)
            return new_name
        return filename

    def _run_audible_download_with_progress(
        self,
        command: list[str],
        progress_callback: Callable[[float], None],
        job_id: int | None = None,
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
            start_new_session=True,
        )
        self._register_process(job_id, process)
        timed_out = threading.Event()

        def stop_for_timeout() -> None:
            timed_out.set()
            self._terminate_process(process)

        timer = threading.Timer(self.config.network_timeout_seconds, stop_for_timeout)
        timer.daemon = True
        timer.start()
        output_lines = []
        try:
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
        finally:
            timer.cancel()
            self._unregister_process(job_id, process)
        output = "".join(output_lines)
        if self._job_cancel_requested(job_id):
            raise JobCancelled("Job wurde während des Downloads abgebrochen")
        if timed_out.is_set():
            raise ProcessTimedOut(
                f"Audible-Download überschritt das Zeitlimit von "
                f"{self.config.network_timeout_seconds} Sekunden"
            )
        if return_code:
            failure = subprocess.CalledProcessError(
                return_code, command, output=output, stderr=output
            )
            raise self._subprocess_failure("Audible-Download", failure) from None
        return subprocess.CompletedProcess(command, return_code, output, output)

    def _download_audio(
        self,
        asin: str,
        work_directory: Path,
        progress_callback: Callable[[float], None] | None = None,
        job_id: int | None = None,
    ) -> list[Path]:
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
            str(min(300, self.config.network_timeout_seconds)),
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
                    timeout=self.config.network_timeout_seconds,
                    start_new_session=True,
                )
            else:
                self._run_audible_download_with_progress(
                    command, progress_callback, job_id=job_id
                )
        except subprocess.CalledProcessError as exc:
            raise self._subprocess_failure("Audible-Download", exc) from None
        except subprocess.TimeoutExpired:
            raise ProcessTimedOut(
                f"Audible-Download überschritt das Zeitlimit von "
                f"{self.config.network_timeout_seconds} Sekunden"
            ) from None
        filenames = sorted(
            path.name
            for path in work_directory.iterdir()
            if path.suffix.lower() in (".aax", ".aaxc", ".mp3")
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
            self._register_secret(license_response["key"])
            self._register_secret(license_response["iv"])
            return [
                "-audible_key",
                license_response["key"],
                "-audible_iv",
                license_response["iv"],
            ]
        return []

    def _command_json(self, command: list[str]) -> dict:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.config.probe_timeout_seconds,
                start_new_session=True,
            )
        except subprocess.CalledProcessError as exc:
            raise self._subprocess_failure("Medienprüfung", exc) from None
        except subprocess.TimeoutExpired:
            raise ProcessTimedOut(
                f"Medienprüfung überschritt das Zeitlimit von "
                f"{self.config.probe_timeout_seconds} Sekunden"
            ) from None
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

    def _run_ffmpeg_with_progress(
        self,
        command: list[str],
        duration: float,
        progress_callback: Callable[[float], None] | None,
        job_id: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Run FFmpeg and report decoded media time as a fraction from zero to one."""
        if progress_callback is None:
            try:
                return subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.config.media_timeout_seconds,
                    start_new_session=True,
                )
            except subprocess.CalledProcessError as exc:
                raise self._subprocess_failure("FFmpeg", exc) from None
            except subprocess.TimeoutExpired:
                raise ProcessTimedOut(
                    f"FFmpeg überschritt das Zeitlimit von "
                    f"{self.config.media_timeout_seconds} Sekunden"
                ) from None
        if duration <= 0:
            try:
                completed = subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.config.media_timeout_seconds,
                    start_new_session=True,
                )
            except subprocess.CalledProcessError as exc:
                raise self._subprocess_failure("FFmpeg", exc) from None
            except subprocess.TimeoutExpired:
                raise ProcessTimedOut(
                    f"FFmpeg überschritt das Zeitlimit von "
                    f"{self.config.media_timeout_seconds} Sekunden"
                ) from None
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
            start_new_session=True,
        )
        self._register_process(job_id, process)
        timed_out = threading.Event()

        def stop_for_timeout() -> None:
            timed_out.set()
            self._terminate_process(process)

        timer = threading.Timer(self.config.media_timeout_seconds, stop_for_timeout)
        timer.daemon = True
        timer.start()
        output_lines = []
        try:
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
        finally:
            timer.cancel()
            self._unregister_process(job_id, process)
        output = "".join(output_lines)
        if self._job_cancel_requested(job_id):
            raise JobCancelled("Job wurde während der Medienverarbeitung abgebrochen")
        if timed_out.is_set():
            raise ProcessTimedOut(
                f"FFmpeg überschritt das Zeitlimit von "
                f"{self.config.media_timeout_seconds} Sekunden"
            )
        if return_code:
            failure = subprocess.CalledProcessError(
                return_code, progress_command, output=output, stderr=output
            )
            raise self._subprocess_failure("FFmpeg", failure) from None
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
        job_id: int | None = None,
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
            try:
                completed = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self.config.media_timeout_seconds,
                    start_new_session=True,
                )
            except subprocess.CalledProcessError as exc:
                raise self._subprocess_failure("FFmpeg-Lautheitsmessung", exc) from None
            except subprocess.TimeoutExpired:
                raise ProcessTimedOut(
                    f"FFmpeg-Lautheitsmessung überschritt das Zeitlimit von "
                    f"{self.config.media_timeout_seconds} Sekunden"
                ) from None
        else:
            completed = self._run_ffmpeg_with_progress(
                command,
                duration,
                progress_callback,
                **({"job_id": job_id} if job_id is not None else {}),
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
    def _temporary_converted_path(destination: Path) -> Path:
        return destination.with_name(
            f".{destination.stem}.audible-convert-{uuid.uuid4().hex}.tmp.m4b"
        )

    @staticmethod
    def _temporary_merged_path(destination: Path) -> Path:
        return destination.with_name(
            f".{destination.stem}.audible-merge-{uuid.uuid4().hex}.tmp.m4b"
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            LOGGER.debug("Could not fsync directory %s", directory)

    def _publish_media_output(
        self,
        temporary: Path,
        destination: Path,
        *,
        overwrite: bool,
        owner_asin: str | None = None,
    ) -> None:
        """Validate, flush, re-check ownership, then atomically publish media."""
        self._validate_media_output(temporary)
        with temporary.open("rb") as output_file:
            os.fsync(output_file.fileno())
        if owner_asin:
            self._reserve_target_path(destination, owner_asin)
        if destination.exists() and not overwrite:
            raise TargetExistsError(
                f"Zieldatei bereits vorhanden: {destination}. "
                "Überschreiben muss bestätigt werden."
            )
        os.replace(temporary, destination)
        self._fsync_directory(destination.parent)

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
        overwrite: bool = False,
        owner_asin: str | None = None,
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
            for index, (part, probe) in enumerate(
                zip(parts, probes, strict=True), start=1
            ):
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
                    **({"job_id": job_id} if job_id is not None else {}),
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
                **({"job_id": job_id} if job_id is not None else {}),
            )
            self._validate_media_output(temporary)
            merged_duration = self._probe_duration(self._probe_audio(temporary))
            tolerance = max(2.0, expected_duration * 0.01)
            if abs(merged_duration - expected_duration) > tolerance:
                raise ProcessingError(
                    "Die Dauer der zusammengeführten M4B weicht von der Summe "
                    "der Teile ab"
                )
            self._publish_media_output(
                temporary,
                destination,
                overwrite=overwrite,
                owner_asin=owner_asin,
            )
            LOGGER.info("%smultipart output published: %s", prefix, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            shutil.rmtree(merge_directory, ignore_errors=True)

    def _transcode_loudnorm(
        self,
        source: Path,
        destination: Path,
        input_options: list[str] | None = None,
        job_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
        overwrite: bool = True,
        owner_asin: str | None = None,
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
                    job_id=job_id,
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
            self._run_ffmpeg_with_progress(
                command,
                duration,
                encoding_progress,
                **({"job_id": job_id} if job_id is not None else {}),
            )
            self._publish_media_output(
                temporary,
                destination,
                overwrite=overwrite,
                owner_asin=owner_asin,
            )
            LOGGER.info("%soutput replaced successfully: %s", prefix, destination)
            return measured
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

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
            ".*.audible-convert-*.tmp.m4b",
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
        owner_asin: str | None = None,
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
                overwrite=overwrite,
                owner_asin=owner_asin,
            )
            source.unlink()
            voucher = source.with_suffix(".voucher")
            if voucher.exists():
                voucher.unlink()
            return
        if destination.exists() and not overwrite:
            raise TargetExistsError(
                f"Zieldatei bereits vorhanden: {destination}. "
                "Überschreiben muss bestätigt werden."
            )
        temporary = self._temporary_converted_path(destination)
        output_options = ["-c", "copy"]
        if source.suffix.lower() == ".mp3":
            audio_probe = self._probe_audio(source)
            output_options = [
                "-map",
                "0:a:0",
                "-map_metadata",
                "0",
                *self._aac_encoding_options(audio_probe),
            ]
        command = [
            "ffmpeg",
            "-y",
            *input_options,
            "-i",
            str(source),
            *output_options,
            str(temporary),
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
        try:
            self._run_ffmpeg_with_progress(
                command,
                duration,
                progress_callback,
                **({"job_id": job_id} if job_id is not None else {}),
            )
            self._publish_media_output(
                temporary,
                destination,
                overwrite=overwrite,
                owner_asin=owner_asin,
            )
            source.unlink()
            voucher = source.with_suffix(".voucher")
            if voucher.exists():
                voucher.unlink()
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

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
        run_id: int | None = None,
        progress_callback: Callable[[float], None] | None = None,
        step_callback: Callable[[str], None] | None = None,
    ) -> Path:
        """Convert one download or merge all multipart downloads into one M4B."""
        if not audio_files:
            raise ProcessingError("Keine heruntergeladenen Audiodateien vorhanden")
        anticipated_bytes = sum(
            source.stat().st_size for source in audio_files if source.is_file()
        ) * (3 if normalize or len(audio_files) > 1 else 2)
        self._ensure_free_space(self.config.audiobook_root, anticipated_bytes)
        if destination.exists() and not overwrite:
            raise TargetExistsError(
                f"Zieldatei bereits vorhanden: {destination}. "
                "Überschreiben muss bestätigt werden."
            )
        self._reserve_target_path(
            destination, book["asin"], job_id=job_id, run_id=run_id
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
                    owner_asin=book["asin"],
                    **extra_options,
                )
            else:
                self._convert_file(
                    audio_files[0],
                    destination,
                    overwrite=overwrite,
                    owner_asin=book["asin"],
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
                overwrite=False if normalize else overwrite,
                owner_asin=None if normalize else book["asin"],
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
                    overwrite=overwrite,
                    owner_asin=book["asin"],
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
        if target_path is None:
            target = self._collision_free_automatic_target(target, asin)
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
            self._reserve_target_path(target, asin, run_id=run_id)
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
            self._ensure_free_space(work_directory)
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
                    run_id=run_id,
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
            with contextlib.suppress(OSError):
                work_directory.rmdir()
            return ProcessResult(
                "success",
                run_id,
                (self.relative_output_path(destination),),
            )
        except Exception as exc:
            message = self._safe_exception_message(exc)
            if run_id is not None:
                self._finish_run(run_id, "error", message)
            LOGGER.error("Verarbeitung von %s fehlgeschlagen: %s", asin, message)
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
            self._ensure_free_space(work_directory)
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
                    job_id=job["id"],
                )
            if self._job_cancel_requested(job["id"]):
                raise JobCancelled("Job wurde abgebrochen")
            if not audio_files:
                raise ProcessingError(
                    f"Audible lieferte keine Audiodatei für {job['asin']}"
                )
            downloaded_bytes = sum(path.stat().st_size for path in audio_files)
            if (
                self.config.max_input_bytes
                and downloaded_bytes > self.config.max_input_bytes
            ):
                raise ProcessingError(
                    "Der Download überschreitet die konfigurierte maximale Eingabegröße."
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
        except JobCancelled as exc:
            self._finish_job(job, "cancelled", self._safe_exception_message(exc))
            self._cleanup_job_work_directory(job)
        except Exception as exc:
            if self._schedule_retry(job, exc, "queued"):
                self._cleanup_job_work_directory(job)
                return True
            message = self._safe_exception_message(exc)
            LOGGER.error("[job %s] download failed: %s", job["id"], message)
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
            run_id=job["run_id"],
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
        for index, (path, weight) in enumerate(
            zip(paths, weights, strict=True), start=1
        ):
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
            if self._job_cancel_requested(job["id"]):
                raise JobCancelled("Job wurde abgebrochen")
            self._finish_job(job, "completed")
            if job["job_type"] != "normalize_existing":
                self._cleanup_job_work_directory(job)
        except JobCancelled as exc:
            self._finish_job(job, "cancelled", self._safe_exception_message(exc))
            if job["job_type"] != "normalize_existing":
                self._cleanup_job_work_directory(job)
        except Exception as exc:
            if self._schedule_retry(job, exc, "waiting_for_processing"):
                return True
            message = self._safe_exception_message(exc)
            LOGGER.error("[job %s] media processing failed: %s", job["id"], message)
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
        with self._process_lock:
            active = list(self._active_processes.values())
        for process in active:
            self._terminate_process(process)
        for thread in self._worker_threads:
            thread.join(timeout=timeout)
        self._worker_threads = []

    def process_pending(self) -> list[EnqueueResult]:
        """Queue eligible new purchases without importing the historical library."""
        with self._connect() as connection:
            asins = [
                row["asin"]
                for row in connection.execute(
                    """SELECT a.asin FROM audiobooks a
                       WHERE COALESCE(a.downloaded, 0)=0
                         AND a.library_state='active'
                         AND date(NULLIF(a.purchase_date, '')) IS NOT NULL
                         AND date(a.purchase_date) >= date(?)
                         AND NOT EXISTS (
                             SELECT 1 FROM processing_runs r
                             WHERE r.asin=a.asin AND r.status='success'
                         )
                       ORDER BY a.rowid""",
                    (self.config.auto_download_from_date,),
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
