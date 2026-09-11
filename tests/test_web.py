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
