from pathlib import Path


def test_existing_file_can_be_linked_without_modifying_it(service):
    path = service.config.audiobook_root / "Eigener Ordner" / "Vorhanden.m4b"
    path.parent.mkdir(parents=True)
    original = b"existing-audio"
    path.write_bytes(original)

    result = service.link_existing_outputs("B012345678", [path])

    assert result.successful
    assert result.outputs == ("Eigener Ordner/Vorhanden.m4b",)
    assert path.read_bytes() == original
    status = service.book_status("B012345678")
    assert status["key"] == "file_exists"
    assert status["normalization"]["key"] == "unknown"
    with service._connect() as connection:
        run = connection.execute(
            "SELECT * FROM processing_runs WHERE id=?", (result.run_id,)
        ).fetchone()
    assert run["operation_type"] == "link_existing"
    assert run["normalization_status"] is None


def test_linked_multipart_files_are_stored_in_stable_order(service):
    folder = service.config.audiobook_root / "Multipart"
    folder.mkdir()
    part_two = folder / "Book - Part 02.m4b"
    part_one = folder / "Book - Part 01.m4b"
    part_two.write_bytes(b"two")
    part_one.write_bytes(b"one")

    result = service.link_existing_outputs(
        "B012345678", [part_two, part_one, part_one]
    )

    assert result.outputs == (
        "Multipart/Book - Part 01.m4b",
        "Multipart/Book - Part 02.m4b",
    )
    with service._connect() as connection:
        rows = connection.execute(
            """SELECT relative_path, part_number FROM output_files
               WHERE run_id=? ORDER BY part_number""",
            (result.run_id,),
        ).fetchall()
    assert [(row["relative_path"], row["part_number"]) for row in rows] == [
        ("Multipart/Book - Part 01.m4b", 1),
        ("Multipart/Book - Part 02.m4b", 2),
    ]


def test_linked_file_can_be_normalized_via_saved_output_path(monkeypatch, service):
    path = service.config.audiobook_root / "custom" / "linked.m4b"
    path.parent.mkdir()
    path.write_bytes(b"existing")
    assert service.link_existing_outputs("B012345678", [path]).successful
    seen = []
    monkeypatch.setattr(
        service,
        "normalize_existing_file",
        lambda value, job_id=None, **kwargs: seen.append(Path(value)),
    )

    queued = service.enqueue_normalize_existing("B012345678")
    assert queued.created
    assert service.run_next_download_job() is False
    assert service.run_next_media_job() is True
    assert seen == [path]


def test_linking_rejects_missing_empty_and_outside_files(service, tmp_path):
    empty = service.config.audiobook_root / "empty.m4b"
    empty.write_bytes(b"")
    outside = tmp_path / "outside.m4b"
    outside.write_bytes(b"audio")

    assert "Mindestens" in service.link_existing_outputs(
        "B012345678", []
    ).error
    assert "fehlt oder ist leer" in service.link_existing_outputs(
        "B012345678", [empty]
    ).error
    assert "innerhalb" in service.link_existing_outputs(
        "B012345678", [outside]
    ).error
    wrong_extension = service.config.audiobook_root / "not-an-audiobook.mp4"
    wrong_extension.write_bytes(b"audio")
    assert "Nur M4B" in service.link_existing_outputs(
        "B012345678", [wrong_extension]
    ).error


def test_file_already_linked_to_another_book_is_rejected(service):
    service.sync_library_rows(
        [{"asin": "B000000002", "title": "Zweites Buch", "authors": "Autor"}]
    )
    path = service.config.audiobook_root / "shared.m4b"
    path.write_bytes(b"audio")
    assert service.link_existing_outputs("B012345678", [path]).successful

    result = service.link_existing_outputs("B000000002", [path])

    assert result.status == "error"
    assert "bereits mit" in result.error
    assert service.book_status("B000000002")["key"] == "not_processed"


def test_discovery_reports_associations_filters_and_hides_temporary_files(service):
    linked = service.config.audiobook_root / "Author" / "Linked.m4b"
    unlinked = service.config.audiobook_root / "Other" / "Unlinked.m4b"
    temporary = service.config.audiobook_root / ".book.audible-normalize-1.tmp.m4b"
    linked.parent.mkdir()
    unlinked.parent.mkdir()
    linked.write_bytes(b"linked")
    unlinked.write_bytes(b"unlinked")
    temporary.write_bytes(b"temporary")
    service.link_existing_outputs("B012345678", [linked])

    all_files = service.discover_existing_m4b_files("B012345678")
    filtered = service.discover_existing_m4b_files("B012345678", query="other")

    assert [item["relative_path"] for item in all_files] == [
        "Author/Linked.m4b",
        "Other/Unlinked.m4b",
    ]
    assert all_files[0]["selected"] is True
    assert all_files[1]["linked_asin"] is None
    assert [item["relative_path"] for item in filtered] == ["Other/Unlinked.m4b"]


def test_relinking_same_file_preserves_known_normalization_state(service):
    path = service.config.audiobook_root / "linked.m4b"
    path.write_bytes(b"audio")
    first = service.link_existing_outputs("B012345678", [path])
    with service._connect() as connection:
        connection.execute(
            """UPDATE output_files SET normalization_status='normalized'
               WHERE run_id=?""",
            (first.run_id,),
        )

    result = service.link_existing_outputs("B012345678", [path])

    assert result.successful
    assert service.book_status("B012345678")["normalization"]["key"] == "normalized"
