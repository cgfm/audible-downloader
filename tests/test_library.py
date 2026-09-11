from pathlib import Path


def write_library(path: Path, title="Neues Buch"):
    path.write_text(
        "asin\ttitle\tsubtitle\tauthors\tseries_title\tnarrators\tseries_sequence\trelease_date\n"
        f"B099999999\t{title}\t\tEine Autorin\t\t\t\t2026-01-01\n",
        encoding="utf-8",
    )


def test_refresh_updates_metadata_without_starting_download(monkeypatch, service, tmp_path):
    library = tmp_path / "library.tsv"
    write_library(library)

    def unexpected_download(*args, **kwargs):
        raise AssertionError("refresh must not download or convert")

    monkeypatch.setattr(service, "_download_audio", unexpected_download)
    first = service.refresh_library(library)
    write_library(library, title="Aktualisierter Titel")
    second = service.refresh_library(library)

    assert first == {"added": 1, "updated": 0}
    assert second == {"added": 0, "updated": 1}
    assert service.get_book("B099999999")["title"] == "Aktualisierter Titel"
    assert service.book_status("B099999999")["key"] == "not_processed"
    with service._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0] == 0


def test_search_covers_title_author_series_and_asin(service):
    assert [book["asin"] for book in service.list_books("Titel")] == ["B012345678"]
    assert [book["asin"] for book in service.list_books("Ada Autor")] == ["B012345678"]
    assert [book["asin"] for book in service.list_books("Meine Serie")] == ["B012345678"]
    assert [book["asin"] for book in service.list_books("B012345678")] == ["B012345678"]
