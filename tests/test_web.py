import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import install_success_pipeline
from web import create_app


def test_library_and_conversion_pages_start(service):
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    library = client.get("/")
    conversion = client.get("/books/B012345678/convert")

    assert library.status_code == 200
    assert "Bibliothek aktualisieren" in library.get_data(as_text=True)
    assert "Der Titel" in library.get_data(as_text=True)
    assert 'http-equiv="refresh"' not in library.get_data(as_text=True)
    assert conversion.status_code == 200
    assert str(service.default_target_path("B012345678")) in conversion.get_data(as_text=True)
    assert "Lautstärke normalisieren" in conversion.get_data(as_text=True)


def test_conversion_post_enqueues_and_returns_immediately(monkeypatch, service):
    def must_not_download(*args, **kwargs):
        raise AssertionError("web request must only enqueue")

    monkeypatch.setattr(service, "_download_audio", must_not_download)
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    response = client.post(
        "/books/B012345678/convert",
        data={
            "target_path": str(service.default_target_path("B012345678")),
            "normalize": "1",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs")
    job = service.list_jobs()[0]
    assert job["status"] == "queued"
    assert job["normalize"] == 1


def test_jobs_page_shows_failure_and_cancel_action(service):
    queued = service.enqueue_conversion("B012345678")
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    page = client.get("/jobs").get_data(as_text=True)
    assert "Der Titel" in page
    assert "Wartet auf Download" in page
    assert "Abbrechen" in page
    response = client.post(f"/jobs/{queued.job_id}/cancel")
    assert response.status_code == 302
    assert service.list_jobs()[0]["status"] == "cancelled"

    finished_page = client.get("/jobs").get_data(as_text=True)
    assert "Entfernen" in finished_page
    response = client.post(f"/jobs/{queued.job_id}/dismiss")
    assert response.status_code == 302
    assert service.list_jobs() == []


def test_job_progress_is_visible_in_jobs_and_library(service):
    service.enqueue_conversion("B012345678")
    job = service._claim_job(
        "queued", "downloading", "Download wird vorbereitet"
    )
    service._start_job_phase(job["id"], "Download (aktuelle Datei)")
    service._update_job_phase_progress(job["id"], 0.42)
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    jobs_page = client.get("/jobs").get_data(as_text=True)
    library_page = client.get("/").get_data(as_text=True)

    assert "Fortschritt" in jobs_page
    assert "0 %" in jobs_page
    assert "ca. 0 %" in library_page
    assert "Download (aktuelle Datei)" in jobs_page
    assert "<span>42 %</span>" in jobs_page
    assert "<span>42 %</span>" in library_page


def test_existing_book_page_offers_persisted_file_normalization(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    assert service.process_book("B012345678").successful
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    page = client.get("/books/B012345678/convert").get_data(as_text=True)
    assert "Vorhandene Dateien normalisieren" in page
    response = client.post("/books/B012345678/normalize")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs")
    assert service.list_jobs()[0]["job_type"] == "normalize_existing"


def test_existing_file_can_be_linked_from_web(service):
    existing = service.config.audiobook_root / "Imported" / "Book.m4b"
    existing.parent.mkdir()
    existing.write_bytes(b"audio")
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    page = client.get("/books/B012345678/link-existing")
    assert page.status_code == 200
    assert "Imported/Book.m4b" in page.get_data(as_text=True)

    response = client.post(
        "/books/B012345678/link-existing",
        data={"paths": "Imported/Book.m4b"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/books/B012345678/convert")
    assert service.book_status("B012345678")["outputs"][0]["relative_path"] == (
        "Imported/Book.m4b"
    )


def test_first_run_redirects_to_audible_setup(service):
    app = create_app(service)
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")
    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "Audible verbinden" in settings.get_data(as_text=True)


def test_settings_can_start_browser_authentication(monkeypatch, service):
    calls = []

    def start(country_code, **options):
        calls.append((country_code, options))
        return {"flow_id": "abc123"}

    monkeypatch.setattr(service, "start_authentication", start)
    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()

    response = client.post(
        "/settings/audible/authenticate",
        data={"country_code": "de"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/settings/audible/authentication/abc123"
    )
    assert calls == [("de", {"with_username": False, "queue_pending": True})]


def test_disabled_automatic_processing_does_not_queue_after_authentication(
    monkeypatch, service
):
    calls = []
    service.update_application_settings({"auto_process_enabled": "False"})
    monkeypatch.setattr(
        service,
        "start_authentication",
        lambda country_code, **options: calls.append((country_code, options))
        or {"flow_id": "abc123"},
    )
    app = create_app(service)
    app.config.update(TESTING=True)

    response = app.test_client().post(
        "/settings/audible/authenticate",
        data={"country_code": "de"},
    )

    assert response.status_code == 302
    assert calls == [("de", {"with_username": False, "queue_pending": False})]


def test_configured_admin_password_protects_web_interface(service):
    app = create_app(service)
    app.config.update(ADMIN_PASSWORD="correct-horse")
    client = app.test_client()

    unauthorized = client.get("/settings")
    credentials = base64.b64encode(b"admin:correct-horse").decode()
    authorized = client.get(
        "/settings", headers={"Authorization": f"Basic {credentials}"}
    )

    assert unauthorized.status_code == 401
    assert 'Basic realm="Audible Downloader"' in unauthorized.headers[
        "WWW-Authenticate"
    ]
    assert authorized.status_code == 200


def test_non_ascii_basic_auth_always_returns_401(service):
    app = create_app(service)
    app.config.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="sicher")
    client = app.test_client()
    credentials = base64.b64encode("ädmin:falsch".encode()).decode()

    response = client.get(
        "/settings", headers={"Authorization": f"Basic {credentials}"}
    )

    assert response.status_code == 401


def test_non_local_start_fails_closed_without_authentication(monkeypatch, service):
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_ADMIN_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("WEB_AUTH_MODE", "basic")

    with pytest.raises(RuntimeError, match="Unsicherer Webstart verweigert"):
        create_app(service, enforce_startup_security=True)


def test_form_login_supports_password_managers_and_logout(monkeypatch, service):
    marker_secret = "FORM_LOGIN_MARKER_SECRET_58f84c"
    monkeypatch.setenv("WEB_AUTH_MODE", "form")
    monkeypatch.setenv("WEB_ADMIN_USERNAME", "nas-user")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", marker_secret)
    monkeypatch.setenv("WEB_SECRET_KEY", "independent-session-key")
    app = create_app(service, enforce_startup_security=True)
    app.config.update(TESTING=True)
    client = app.test_client()

    protected = client.get("/settings")
    login_page = client.get(protected.headers["Location"])
    wrong_password = client.post(
        "/login",
        data={
            "username": "nas-user",
            "password": "wrong",
            "next": "/settings",
        },
    )
    accepted = client.post(
        "/login",
        data={
            "username": "nas-user",
            "password": marker_secret,
            "next": "/settings",
        },
    )

    assert protected.status_code == 302
    assert protected.headers["Location"].endswith("/login?next=/settings")
    assert login_page.status_code == 200
    assert 'autocomplete="username"' in login_page.get_data(as_text=True)
    assert 'autocomplete="current-password"' in login_page.get_data(as_text=True)
    assert marker_secret not in login_page.get_data(as_text=True)
    assert wrong_password.status_code == 401
    assert marker_secret not in wrong_password.get_data(as_text=True)
    assert accepted.status_code == 302
    assert accepted.headers["Location"].endswith("/settings")
    assert marker_secret not in accepted.headers.get("Set-Cookie", "")
    with client.session_transaction() as browser_session:
        assert marker_secret not in repr(dict(browser_session))
    assert client.get("/settings").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/settings").status_code == 302


def test_form_login_requires_csrf_and_rejects_external_redirect(monkeypatch, service):
    monkeypatch.setenv("WEB_AUTH_MODE", "form")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "correct-horse")
    monkeypatch.setenv("WEB_SECRET_KEY", "independent-session-key")
    app = create_app(service, enforce_startup_security=True)
    client = app.test_client()

    assert client.post(
        "/login",
        data={"username": "admin", "password": "correct-horse"},
    ).status_code == 400

    client.get("/login")
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    accepted = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "correct-horse",
            "_csrf_token": token,
            "next": "https://attacker.invalid/steal",
        },
    )

    assert accepted.status_code == 302
    assert accepted.headers["Location"].endswith("/")
    assert "attacker.invalid" not in accepted.headers["Location"]


def test_form_mode_requires_password_even_for_local_start(monkeypatch, service):
    monkeypatch.setenv("WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("WEB_AUTH_MODE", "form")
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_ADMIN_PASSWORD_FILE", raising=False)

    with pytest.raises(RuntimeError, match="kein Passwort"):
        create_app(service, enforce_startup_security=True)


def test_wsgi_reports_invalid_web_configuration_without_traceback(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "AUDIBLE_CONFIG_DIR": str(tmp_path / "config"),
        "AUDIOBOOK_DIRECTORY": str(tmp_path / "audiobooks"),
        "AUDIBLE_WORK_DIRECTORY": str(tmp_path / "work"),
        "WEB_HOST": "0.0.0.0",
        "WEB_AUTH_MODE": "form",
        "WEB_ADMIN_PASSWORD": "",
        "WEB_ADMIN_PASSWORD_FILE": "",
        "LOG_FORMAT": "text",
    }

    result = subprocess.run(
        [sys.executable, str(repository / "app" / "wsgi.py")],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 78
    assert "Start abgebrochen" in output
    assert "WEB_AUTH_MODE=form" in output
    assert "WEB_ADMIN_PASSWORD" in output
    assert "Traceback" not in output


def test_admin_password_can_be_loaded_from_secret_file(monkeypatch, service, tmp_path):
    secret_file = tmp_path / "web-password"
    secret_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("WEB_ADMIN_PASSWORD_FILE", str(secret_file))
    app = create_app(service)
    client = app.test_client()
    credentials = base64.b64encode(b"admin:from-file").decode()

    assert client.get("/settings").status_code == 401
    assert client.get(
        "/settings", headers={"Authorization": f"Basic {credentials}"}
    ).status_code == 200


def test_dynamic_pages_are_not_cached_and_health_is_public(service):
    app = create_app(service)
    app.config.update(ADMIN_PASSWORD="correct-horse")
    client = app.test_client()

    assert client.get("/healthz").status_code == 200
    credentials = base64.b64encode(b"admin:correct-horse").decode()
    response = client.get(
        "/settings", headers={"Authorization": f"Basic {credentials}"}
    )
    assert response.headers["Cache-Control"] == "no-store"


def test_post_requests_require_csrf_token_outside_tests(service):
    app = create_app(service)
    client = app.test_client()

    missing_token = client.post(
        "/settings/audible/authenticate", data={"country_code": "de"}
    )
    settings = client.get("/settings")
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    accepted = client.post(
        "/settings/audible/authenticate",
        data={"country_code": "invalid", "_csrf_token": token},
    )

    assert missing_token.status_code == 400
    assert settings.status_code == 200
    assert accepted.status_code == 302
