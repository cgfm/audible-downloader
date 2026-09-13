from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import web
from web import automatic_scheduler, create_app, run_automatic_cycle


def application_settings_payload(service, **overrides):
    values = {
        key: setting["value"]
        for key, setting in service.application_settings().items()
    }
    values.update(overrides)
    return values


def test_automatic_processing_pause_is_persistent_and_skips_queue(
    monkeypatch, service_factory
):
    service = service_factory()
    service.set_automatic_processing_paused(True)
    monkeypatch.setattr(
        service,
        "audible_authentication_status",
        lambda: {"connected": True},
    )
    calls = []
    monkeypatch.setattr(service, "refresh_library", lambda: calls.append("refresh"))
    monkeypatch.setattr(
        service, "reconcile_existing_outputs", lambda: calls.append("reconcile")
    )
    monkeypatch.setattr(service, "process_pending", lambda: calls.append("process"))

    run_automatic_cycle(service)

    assert calls == ["refresh", "reconcile"]
    assert service.operational_status()["last_status"] == "paused"
    assert service_factory().automatic_processing_paused() is True


def test_scheduler_reacts_when_automatic_processing_is_enabled(monkeypatch):
    class SchedulerStopped(Exception):
        pass

    calls = []
    service = SimpleNamespace(
        config=SimpleNamespace(
            automatic_processing_enabled=False,
            automatic_process_interval=600,
        )
    )

    def wait_for_change(timeout):
        calls.append(("wait", timeout))
        if len(calls) == 1:
            service.config.automatic_processing_enabled = True
            return True
        raise SchedulerStopped

    service.wait_for_application_settings_change = wait_for_change
    monkeypatch.setattr(web, "run_automatic_cycle", lambda current: calls.append("run"))

    with pytest.raises(SchedulerStopped):
        automatic_scheduler(service)

    assert calls[0] == ("wait", 3600)
    assert calls[1] == "run"


def test_interval_change_does_not_trigger_an_extra_immediate_cycle(monkeypatch):
    class SchedulerStopped(Exception):
        pass

    runs = []
    waits = []
    service = SimpleNamespace(
        config=SimpleNamespace(
            automatic_processing_enabled=True,
            automatic_process_interval=600,
        )
    )

    def wait_for_change(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            service.config.automatic_process_interval = 1200
            return True
        raise SchedulerStopped

    service.wait_for_application_settings_change = wait_for_change
    monkeypatch.setattr(web, "run_automatic_cycle", lambda current: runs.append(current))

    with pytest.raises(SchedulerStopped):
        automatic_scheduler(service)

    assert runs == [service]
    assert 599 <= waits[0] <= 600
    assert 1199 <= waits[1] <= 1200


def test_settings_can_resume_automatic_processing(service):
    service.set_automatic_processing_paused(True)
    app = create_app(service)
    app.config.update(TESTING=True)

    response = app.test_client().post(
        "/settings/automatic-processing", data={"paused": "0"}
    )

    assert response.status_code == 302
    assert service.automatic_processing_paused() is False


def test_first_initialization_persists_current_automatic_download_date(
    service_factory,
):
    today = datetime.now(UTC).date().isoformat()
    service = service_factory()

    assert service.config.auto_download_from_date == today
    with service._connect() as connection:
        stored = connection.execute(
            "SELECT value FROM runtime_state WHERE key='auto_download_from_date'"
        ).fetchone()["value"]

    assert stored == today
    assert service_factory().config.auto_download_from_date == stored


def test_application_settings_are_editable_and_persistent(service_factory):
    service = service_factory()
    app = create_app(service)
    app.config.update(TESTING=True)
    payload = application_settings_payload(
        service,
        auto_download_from_date="2026-08-01",
        audiobook_folders="False",
        single_book_path_template="{author}/{asin} - {title}.m4b",
        series_book_path_template="{series}/{asin} - {title}.m4b",
        auto_process_enabled="False",
        auto_process_interval="600",
        log_level="WARNING",
    )

    response = app.test_client().post("/settings/application", data=payload)

    assert response.status_code == 302
    assert service.config.auto_download_from_date == "2026-08-01"
    assert service.config.use_folders is False
    assert service.config.single_book_path_template == payload[
        "single_book_path_template"
    ]
    assert service.config.series_book_path_template == payload[
        "series_book_path_template"
    ]
    assert service.config.automatic_processing_enabled is False
    assert service.config.automatic_process_interval == 600
    assert service.config.log_level == "WARNING"

    restarted = service_factory()
    assert restarted.config.auto_download_from_date == "2026-08-01"
    assert restarted.config.use_folders is False
    assert restarted.config.automatic_processing_enabled is False
    assert restarted.config.automatic_process_interval == 600
    assert restarted.config.log_level == "WARNING"


def test_environment_settings_lock_web_fields_and_cannot_be_overwritten(
    monkeypatch, service_factory
):
    environment = {
        "AUTO_DOWNLOAD_FROM_DATE": "2026-09-01",
        "AUDIOBOOK_FOLDERS": "False",
        "SINGLE_BOOK_PATH_TEMPLATE": "env/{asin} - {title}.m4b",
        "SERIES_BOOK_PATH_TEMPLATE": "env/{series}/{asin} - {title}.m4b",
        "AUTO_PROCESS_ENABLED": "False",
        "AUTO_PROCESS_INTERVAL": "900",
        "LOG_LEVEL": "INFO",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    service = service_factory()
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    page = client.get("/settings").get_data(as_text=True)
    for name in environment:
        assert f"Durch <code>{name}</code> festgelegt" in page
    assert page.count("disabled") >= len(environment)

    response = client.post(
        "/settings/application",
        data={
            "auto_download_from_date": "2000-01-01",
            "audiobook_folders": "True",
            "single_book_path_template": "ui/{title}.m4b",
            "series_book_path_template": "ui/{title}.m4b",
            "auto_process_enabled": "True",
            "auto_process_interval": "60",
            "log_level": "CRITICAL",
        },
    )

    assert response.status_code == 302
    settings = service.application_settings()
    assert {
        setting["environment_name"]: setting["value"]
        for setting in settings.values()
    } == environment
    assert all(setting["environment_locked"] for setting in settings.values())


def test_removing_environment_override_restores_database_value(
    monkeypatch, service_factory
):
    service = service_factory()
    service.update_application_settings({"auto_download_from_date": "2025-01-01"})
    monkeypatch.setenv("AUTO_DOWNLOAD_FROM_DATE", "2026-09-01")

    overridden = service_factory()
    assert overridden.config.auto_download_from_date == "2026-09-01"

    monkeypatch.delenv("AUTO_DOWNLOAD_FROM_DATE")
    restored = service_factory()
    assert restored.config.auto_download_from_date == "2025-01-01"


def test_invalid_application_setting_is_not_persisted(service):
    original = service.config.single_book_path_template
    app = create_app(service)
    app.config.update(TESTING=True)
    payload = application_settings_payload(
        service,
        single_book_path_template="{unknown}/{title}.m4b",
    )

    response = app.test_client().post("/settings/application", data=payload)

    assert response.status_code == 302
    assert service.config.single_book_path_template == original


def test_manual_database_backup_is_consistent_and_private(service):
    backup = service.create_database_backup()

    assert backup.parent == service.config.config_directory / "backups"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    assert backup.read_bytes().startswith(b"SQLite format 3")


def test_history_retention_keeps_published_path_claim(service):
    target = service.default_target_path("B012345678")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    run_id = service._create_run(
        "B012345678", target, False, False, False, "test"
    )
    service._record_output(run_id, target, None)
    service._finish_run(run_id, "success")
    queued = service.enqueue_normalize_existing("B012345678")
    job = service._claim_job(
        "waiting_for_processing", "processing", "Normalisierung"
    )
    service._finish_job(job, "completed")
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat(
        timespec="seconds"
    )
    with service._connect() as connection:
        connection.execute(
            "UPDATE jobs SET dismissed_at=?, finished_at=? WHERE id=?",
            (old, old, queued.job_id),
        )

    assert service.prune_history(5) == 1
    with service._connect() as connection:
        claim = connection.execute(
            "SELECT asin FROM target_path_claims WHERE relative_path=?",
            (service.relative_output_path(target),),
        ).fetchone()
    assert claim["asin"] == "B012345678"


def test_metrics_report_books_jobs_and_automatic_state(service):
    service.enqueue_conversion("B012345678")
    service.record_automatic_cycle("success", started_at="2026-01-01T00:00:00+00:00")
    app = create_app(service)
    app.config.update(TESTING=True)

    response = app.test_client().get("/metrics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["books"]["active"] == 1
    assert payload["jobs"]["queued"] == 1
    assert payload["automatic"]["last_status"] == "success"
