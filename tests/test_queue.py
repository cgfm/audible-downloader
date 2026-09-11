import json
import threading
import time
from pathlib import Path


def add_book(service, asin, title):
    service.sync_library_rows([{"asin": asin, "title": title, "authors": "Autor"}])


def install_queue_pipeline(monkeypatch, service, fail_asin=None, events=None):
    events = events if events is not None else []

    def download(asin, work_directory, progress_callback=None):
        if progress_callback:
            progress_callback(1.0)
        events.append(("download", asin))
        source = work_directory / f"{asin}.aax"
        source.write_bytes(b"source")
        return [source]

    def convert(source, destination, overwrite=False, **kwargs):
        asin = source.stem
        events.append(("media", asin))
        if asin == fail_asin:
            raise RuntimeError(f"conversion failed for {asin}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"converted")
        source.unlink()

    monkeypatch.setattr(service, "_download_audio", download)
    monkeypatch.setattr(service, "_convert_file", convert)
    return events


def drain_queue(service):
    while service.run_next_download_job():
        pass
    while service.run_next_media_job():
        pass


def test_job_options_and_custom_target_are_persisted(service):
    target = service.config.audiobook_root / "custom" / "book.m4b"
    queued = service.enqueue_conversion(
        "B012345678", target, overwrite=True, normalize=True
    )
    with service._connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (queued.job_id,)).fetchone()
        run = connection.execute(
            "SELECT * FROM processing_runs WHERE id=?", (job["run_id"],)
        ).fetchone()
    assert job["status"] == "queued"
    assert job["normalize"] == 1
    assert job["overwrite"] == 1
    assert job["requested_target_path"] == "custom/book.m4b"
    assert run["normalization_requested"] == 1
    assert run["operation_type"] == "convert"
    assert run["job_id"] == queued.job_id


def test_multiple_jobs_are_processed_fifo(monkeypatch, service):
    add_book(service, "B000000002", "Zwei")
    add_book(service, "B000000003", "Drei")
    events = install_queue_pipeline(monkeypatch, service)
    for asin in ("B012345678", "B000000002", "B000000003"):
        assert service.enqueue_conversion(asin).created
    drain_queue(service)
    assert [event[1] for event in events if event[0] == "download"] == [
        "B012345678", "B000000002", "B000000003"
    ]
    assert [event[1] for event in events if event[0] == "media"] == [
        "B012345678", "B000000002", "B000000003"
    ]


def test_failed_job_does_not_stop_queue(monkeypatch, service):
    add_book(service, "B000000002", "Zwei")
    install_queue_pipeline(monkeypatch, service, fail_asin="B012345678")
    first = service.enqueue_conversion("B012345678")
    second = service.enqueue_conversion("B000000002")
    drain_queue(service)
    by_id = {job["id"]: job for job in service.list_jobs()}
    assert by_id[first.job_id]["status"] == "failed"
    assert by_id[second.job_id]["status"] == "completed"


def test_queued_job_survives_service_restart(service_factory):
    first_service = service_factory()
    add_book(first_service, "B000000001", "Eins")
    queued = first_service.enqueue_conversion("B000000001")
    second_service = service_factory()
    job = next(job for job in second_service.list_jobs() if job["id"] == queued.job_id)
    assert job["status"] == "queued"


def test_active_jobs_are_marked_interrupted_after_restart(service_factory):
    first_service = service_factory()
    add_book(first_service, "B000000001", "Eins")
    queued = first_service.enqueue_conversion("B000000001")
    with first_service._connect() as connection:
        connection.execute(
            "UPDATE jobs SET status='processing' WHERE id=?", (queued.job_id,)
        )
        connection.execute(
            "UPDATE processing_runs SET status='running' WHERE job_id=?",
            (queued.job_id,),
        )
    second_service = service_factory()
    job = next(job for job in second_service.list_jobs() if job["id"] == queued.job_id)
    assert job["status"] == "failed"
    assert "Neustart unterbrochen" in job["error_message"]
    with second_service._connect() as connection:
        run = connection.execute(
            "SELECT status, error_message FROM processing_runs WHERE job_id=?",
            (queued.job_id,),
        ).fetchone()
    assert run["status"] == "error"
    assert "Neustart unterbrochen" in run["error_message"]


def test_downloaded_job_waiting_for_media_survives_restart(
    monkeypatch, service_factory
):
    first_service = service_factory()
    add_book(first_service, "B000000001", "Eins")
    install_queue_pipeline(monkeypatch, first_service)
    queued = first_service.enqueue_conversion("B000000001")
    assert first_service.run_next_download_job()

    second_service = service_factory()
    converted = []

    def convert(source, destination, overwrite=False, **kwargs):
        converted.append(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"converted")
        source.unlink()

    monkeypatch.setattr(second_service, "_convert_file", convert)
    assert second_service.run_next_media_job()
    assert converted
    job = next(job for job in second_service.list_jobs() if job["id"] == queued.job_id)
    assert job["status"] == "completed"


def test_waiting_job_with_missing_staged_input_is_failed_on_restart(
    monkeypatch, service_factory
):
    first_service = service_factory()
    add_book(first_service, "B000000001", "Eins")
    install_queue_pipeline(monkeypatch, first_service)
    queued = first_service.enqueue_conversion("B000000001")
    assert first_service.run_next_download_job()
    with first_service._connect() as connection:
        staged = json.loads(
            connection.execute(
                "SELECT downloaded_files FROM jobs WHERE id=?", (queued.job_id,)
            ).fetchone()[0]
        )
    Path(staged[0]).unlink()

    second_service = service_factory()
    job = next(job for job in second_service.list_jobs() if job["id"] == queued.job_id)
    assert job["status"] == "failed"
    assert "Neustart unterbrochen" in job["error_message"]


def test_double_click_does_not_create_duplicate_active_job(service):
    first = service.enqueue_conversion("B012345678")
    second = service.enqueue_conversion("B012345678")
    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id
    with service._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_waiting_job_can_be_cancelled(service):
    queued = service.enqueue_conversion("B012345678")
    assert service.cancel_job(queued.job_id) is True
    assert service.cancel_job(queued.job_id) is False
    with service._connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (queued.job_id,)).fetchone()
        run = connection.execute("SELECT * FROM processing_runs WHERE job_id=?", (queued.job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert job["finished_at"]
    assert run["status"] == "cancelled"


def test_only_terminal_jobs_can_be_dismissed_without_deleting_history(service):
    queued = service.enqueue_conversion("B012345678")
    assert service.dismiss_job(queued.job_id) is False
    assert service.cancel_job(queued.job_id) is True

    assert service.dismiss_job(queued.job_id) is True
    assert service.list_jobs() == []
    assert service.dismiss_job(queued.job_id) is False
    with service._connect() as connection:
        job = connection.execute(
            "SELECT dismissed_at FROM jobs WHERE id=?", (queued.job_id,)
        ).fetchone()
        run = connection.execute(
            "SELECT status FROM processing_runs WHERE job_id=?", (queued.job_id,)
        ).fetchone()
    assert job["dismissed_at"]
    assert run["status"] == "cancelled"


def test_job_failure_status_and_error_are_stored(monkeypatch, service):
    install_queue_pipeline(monkeypatch, service, fail_asin="B012345678")
    queued = service.enqueue_conversion("B012345678")
    drain_queue(service)
    with service._connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (queued.job_id,)).fetchone()
        run = connection.execute("SELECT * FROM processing_runs WHERE job_id=?", (queued.job_id,)).fetchone()
    assert job["status"] == "failed"
    assert job["finished_at"]
    assert job["error_message"] == "conversion failed for B012345678"
    assert run["status"] == "error"
    assert run["error_message"] == job["error_message"]


def test_at_most_one_download_runs_at_a_time(monkeypatch, service):
    add_book(service, "B000000002", "Zwei")
    service.enqueue_conversion("B012345678")
    service.enqueue_conversion("B000000002")
    guard = threading.Lock()
    active = 0
    maximum = 0

    def download(asin, work_directory, progress_callback=None):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        source = work_directory / f"{asin}.aax"
        source.write_bytes(b"source")
        with guard:
            active -= 1
        return [source]

    monkeypatch.setattr(service, "_download_audio", download)
    threads = [threading.Thread(target=service.run_next_download_job) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1


def test_at_most_one_media_operation_runs_at_a_time(monkeypatch, service):
    add_book(service, "B000000002", "Zwei")
    install_queue_pipeline(monkeypatch, service)
    service.enqueue_conversion("B012345678")
    service.enqueue_conversion("B000000002")
    while service.run_next_download_job():
        pass
    guard = threading.Lock()
    active = 0
    maximum = 0

    def convert(source, destination, overwrite=False, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"converted")
        source.unlink()
        with guard:
            active -= 1

    monkeypatch.setattr(service, "_convert_file", convert)
    threads = [threading.Thread(target=service.run_next_media_job) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1


def test_download_can_overlap_media_processing_of_another_job(monkeypatch, service):
    add_book(service, "B000000002", "Zwei")
    service.enqueue_conversion("B012345678")
    service.enqueue_conversion("B000000002")
    second_download_active = threading.Event()
    release_second_download = threading.Event()
    overlap_observed = threading.Event()

    def download(asin, work_directory, progress_callback=None):
        source = work_directory / f"{asin}.aax"
        source.write_bytes(b"source")
        if asin == "B000000002":
            second_download_active.set()
            release_second_download.wait(2)
        return [source]

    def convert(source, destination, overwrite=False, **kwargs):
        if source.stem == "B012345678" and second_download_active.wait(2):
            overlap_observed.set()
        release_second_download.set()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"converted")
        source.unlink()

    monkeypatch.setattr(service, "_download_audio", download)
    monkeypatch.setattr(service, "_convert_file", convert)
    service.start_job_workers()
    try:
        assert overlap_observed.wait(3)
    finally:
        release_second_download.set()
        service.stop_job_workers()


def test_normalization_job_starts_in_media_queue(service):
    target = service.config.audiobook_root / "saved.m4b"
    target.write_bytes(b"existing")
    run_id = service._create_run(
        "B012345678", target, True, False, False, "test"
    )
    service._record_output(run_id, target, None)
    service._finish_run(run_id, "success")
    queued = service.enqueue_normalize_existing("B012345678")
    with service._connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (queued.job_id,)).fetchone()
    assert job["status"] == "waiting_for_processing"
    assert job["downloaded_files"] is None


def test_process_pending_uses_persistent_queue(service):
    results = service.process_pending()
    assert len(results) == 1
    assert results[0].created
    assert service.list_jobs()[0]["status"] == "queued"
