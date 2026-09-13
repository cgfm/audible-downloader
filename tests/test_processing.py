import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from conftest import install_success_pipeline


def test_manual_target_is_used_and_stored_as_override(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    target = service.config.audiobook_root / "Eigener Ordner" / "Wunschname.m4b"
    result = service.process_book("B012345678", target_path=target, trigger="web")
    assert result.successful
    assert result.outputs == ("Eigener Ordner/Wunschname.m4b",)
    with service._connect() as connection:
        run = connection.execute(
            "SELECT * FROM processing_runs WHERE id=?", (result.run_id,)
        ).fetchone()
    assert run["target_overridden"] == 1
    assert run["requested_target_path"] == "Eigener Ordner/Wunschname.m4b"


def test_processed_book_is_skipped_without_force(monkeypatch, service):
    calls = install_success_pipeline(monkeypatch, service)
    assert service.process_book("B012345678").successful
    call_count = len(calls)
    result = service.process_book("B012345678")
    assert result.status == "skipped"
    assert len(calls) == call_count


def test_processed_book_runs_again_with_force(monkeypatch, service):
    calls = install_success_pipeline(monkeypatch, service)
    assert service.process_book("B012345678").successful
    second_target = service.config.audiobook_root / "new" / "again.m4b"
    result = service.process_book(
        "B012345678", target_path=second_target, force=True, trigger="web"
    )
    assert result.successful
    assert sum(call[0] == "download" for call in calls) == 2
    with service._connect() as connection:
        successes = connection.execute(
            "SELECT COUNT(*) FROM processing_runs WHERE asin=? AND status='success'",
            ("B012345678",),
        ).fetchone()[0]
    assert successes == 2


def test_existing_target_is_rejected_without_overwrite(monkeypatch, service):
    calls = install_success_pipeline(monkeypatch, service)
    target = service.default_target_path("B012345678")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    result = service.process_book("B012345678")
    assert result.status == "error"
    assert "bereits vorhanden" in result.error
    assert calls == []
    assert target.read_bytes() == b"existing"


def test_existing_target_can_be_explicitly_overwritten(monkeypatch, service):
    calls = install_success_pipeline(monkeypatch, service)
    target = service.default_target_path("B012345678")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    result = service.process_book("B012345678", overwrite=True)
    assert result.successful
    assert target.read_bytes().startswith(b"converted")
    assert any(call[0] == "convert" and call[2] is True for call in calls)


def test_multipart_download_is_merged_and_one_output_is_persisted(monkeypatch, service):
    calls = install_success_pipeline(monkeypatch, service, parts=2)
    result = service.process_book("B012345678")
    assert result.outputs == (
        "Ada Autor/Meine Serie/Band 1 - Der Titel/B012345678 - Der Titel.m4b",
    )
    assert [call[0] for call in calls] == ["download", "convert", "convert", "merge"]
    with service._connect() as connection:
        outputs = connection.execute(
            "SELECT relative_path, part_number FROM output_files WHERE run_id=? ORDER BY part_number",
            (result.run_id,),
        ).fetchall()
    assert len(outputs) == 1
    assert outputs[0]["part_number"] is None


def test_multipart_download_uses_exact_custom_target(monkeypatch, service):
    install_success_pipeline(monkeypatch, service, parts=2)
    target = service.config.audiobook_root / "Eigener Ordner" / "Gesamt.m4b"

    result = service.process_book("B012345678", target_path=target)

    assert result.successful
    assert result.outputs == ("Eigener Ordner/Gesamt.m4b",)
    assert target.is_file()


def test_multipart_is_normalized_only_after_parts_are_merged(monkeypatch, service):
    calls = install_success_pipeline(monkeypatch, service, parts=2)
    normalized = []

    def normalize(source, destination, **kwargs):
        normalized.append((source, destination))
        assert source.name == "merged.m4b"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"normalized-complete-book")

    monkeypatch.setattr(service, "_transcode_loudnorm", normalize)

    result = service.process_book("B012345678", normalize=True)

    assert result.successful
    assert len(normalized) == 1
    assert [call[0] for call in calls] == ["download", "convert", "convert", "merge"]
    assert service.book_status("B012345678")["normalization"]["key"] == "normalized"


def test_success_updates_history_and_file_status(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    result = service.process_book("B012345678")
    status = service.book_status("B012345678")
    assert result.successful
    assert status["key"] == "file_exists"
    assert status["has_success_history"] is True
    assert status["outputs"][0]["exists"] is True
    assert status["normalization"]["key"] == "not_normalized"
    with service._connect() as connection:
        downloaded = connection.execute(
            "SELECT downloaded FROM audiobooks WHERE asin=?", ("B012345678",)
        ).fetchone()[0]
    assert downloaded == 1


def test_failure_is_visible_and_error_is_stored(monkeypatch, service):
    def fail_download(asin, work_directory):
        raise RuntimeError("Audible ist nicht erreichbar")

    monkeypatch.setattr(service, "_download_audio", fail_download)
    result = service.process_book("B012345678")
    status = service.book_status("B012345678")
    assert result.status == "error"
    assert status["key"] == "error"
    assert status["error_message"] == "Audible ist nicht erreichbar"
    with service._connect() as connection:
        run = connection.execute(
            "SELECT status, finished_at, error_message FROM processing_runs WHERE id=?",
            (result.run_id,),
        ).fetchone()
    assert run["status"] == "error"
    assert run["finished_at"]
    assert run["error_message"] == "Audible ist nicht erreichbar"


def test_status_uses_saved_output_not_changed_template(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    target = service.config.audiobook_root / "old" / "saved.m4b"
    assert service.process_book("B012345678", target_path=target).successful
    object.__setattr__(service.config, "series_book_path_template", "new/{title}.m4b")
    status = service.book_status("B012345678")
    assert status["key"] == "file_exists"
    assert status["outputs"][0]["relative_path"] == "old/saved.m4b"


def test_flat_automatic_mode_keeps_audible_filename(monkeypatch, service_factory):
    service = service_factory(use_folders=False)
    service.sync_library_rows(
        [{"asin": "B012345678", "title": "Der Titel", "authors": "Ada Autor"}]
    )
    calls = install_success_pipeline(monkeypatch, service)
    result = service.process_book("B012345678")
    assert result.outputs == ("B012345678_part1.m4b",)
    assert calls[1][1].name == "B012345678_part1.m4b"


def test_internal_aaxc_id_and_its_matching_voucher_are_renamed_together(service):
    work = service._work_directory("B012345678")
    source = work / "INTERNAL01_audio.aaxc"
    voucher = work / "INTERNAL01_audio.voucher"
    source.write_bytes(b"aaxc")
    voucher.write_text("{}", encoding="utf-8")

    renamed = service._normalize_downloaded_filename(
        work, source.name, "B012345678"
    )

    assert renamed == "B012345678_audio.aaxc"
    assert (work / renamed).exists()
    assert (work / "B012345678_audio.voucher").exists()


def test_aaxc_conversion_uses_voucher_with_same_basename(monkeypatch, service):
    work = service._work_directory("B012345678")
    source = work / "B012345678_audio.aaxc"
    destination = service.config.audiobook_root / "converted.m4b"
    source.write_bytes(b"aaxc")
    source.with_suffix(".voucher").write_text(
        json.dumps(
            {
                "content_license": {
                    "license_response": {"key": "exact-key", "iv": "exact-iv"}
                }
            }
        ),
        encoding="utf-8",
    )
    commands = []

    def fake_run(command, check=True, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"converted")

    monkeypatch.setattr("audible_service.subprocess.run", fake_run)
    monkeypatch.setattr(service, "_validate_media_output", lambda path: None)
    service._convert_file(source, destination)

    assert "exact-key" in commands[0]
    assert "exact-iv" in commands[0]
    assert not source.with_suffix(".voucher").exists()


def test_foreign_owned_target_cannot_be_enqueued_with_overwrite(service):
    target = service.config.audiobook_root / "shared.m4b"
    target.write_bytes(b"book-a")
    assert service.link_existing_outputs("B012345678", [target]).successful
    service.sync_library_rows(
        [{"asin": "B000000002", "title": "Buch B", "authors": "Autor"}]
    )

    queued = service.enqueue_conversion(
        "B000000002", target_path=target, overwrite=True
    )

    assert queued.created is False
    assert queued.status == "failed"
    assert "gehört bereits" in queued.error
    assert target.read_bytes() == b"book-a"


def test_target_claim_migration_blocks_foreign_owner(service_factory):
    service = service_factory()
    service.sync_library_rows(
        [
            {"asin": "B000000001", "title": "Buch A", "authors": "Autor"},
            {"asin": "B000000002", "title": "Buch B", "authors": "Autor"},
        ]
    )
    target = service.config.audiobook_root / "historical.m4b"
    target.write_bytes(b"book-a")
    run_id = service._create_run(
        "B000000001", target, False, False, False, "migration-test"
    )
    service._record_output(run_id, target, None)
    service._finish_run(run_id, "success")
    with service._connect() as connection:
        connection.execute("DROP TABLE target_path_claims")

    migrated = service_factory()
    with migrated._connect() as connection:
        claim = connection.execute(
            "SELECT asin, state FROM target_path_claims WHERE relative_path=?",
            (migrated.relative_output_path(target),),
        ).fetchone()
    queued = migrated.enqueue_conversion(
        "B000000002", target_path=target, overwrite=True
    )

    assert dict(claim) == {"asin": "B000000001", "state": "published"}
    assert queued.created is False
    assert "gehört bereits" in queued.error
    assert target.read_bytes() == b"book-a"


def test_automatic_path_collision_gets_asin_suffix(service_factory):
    service = service_factory(
        use_folders=True,
        single_book_path_template="{author}/{title}/{title}.m4b",
    )
    service.sync_library_rows(
        [
            {"asin": "B000000001", "title": "Gleich", "authors": "Autor"},
            {"asin": "B000000002", "title": "Gleich", "authors": "Autor"},
        ]
    )
    first = service.enqueue_conversion("B000000001")
    second = service.enqueue_conversion("B000000002")

    assert first.created and second.created
    jobs = {job["asin"]: job for job in service.list_jobs()}
    assert jobs["B000000001"]["requested_target_path"] == "Autor/Gleich/Gleich.m4b"
    assert jobs["B000000002"]["requested_target_path"] == (
        "Autor/Gleich/Gleich [B000000002].m4b"
    )


def test_legacy_database_is_migrated_without_losing_downloaded(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    database = config / "audiobooks.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE audiobooks (
                asin TEXT UNIQUE, title TEXT NOT NULL, subtitle TEXT,
                authors TEXT NOT NULL, series_title TEXT, narrators TEXT,
                series_sequence INT, release_date TEXT, downloaded INT
            )"""
        )
        connection.execute(
            "INSERT INTO audiobooks VALUES (?, ?, '', ?, '', '', '', '', 1)",
            ("B012345678", "Alt", "Autor"),
        )
    from audible_service import AudibleService, ServiceConfig

    service = AudibleService(
        ServiceConfig(
            config_directory=config,
            audiobook_root=tmp_path / "audiobooks",
            work_root=tmp_path / "work",
            use_folders=True,
        )
    )
    migrated = service.get_book("B012345678")
    assert migrated["downloaded"] == 1
    assert migrated["library_state"] == "active"
    assert "last_seen_at" in migrated
    assert service.config.auto_download_from_date == datetime.now(UTC).date().isoformat()
    assert service.book_status("B012345678")["key"] == "history"
    backups = list((config / "backups").glob("audiobooks-startup-*.db"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_legacy_output_is_imported_with_its_actual_path(service):
    target = service.default_target_path("B012345678")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"legacy")

    assert service.reconcile_existing_outputs() == 1
    status = service.book_status("B012345678")
    assert status["key"] == "file_exists"
    assert status["outputs"][0]["relative_path"] == service.relative_output_path(target)
    assert status["normalization"]["key"] == "unknown"
