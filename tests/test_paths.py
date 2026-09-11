from audible_service import AudibleService, ServiceConfig


def add_book(service, **overrides):
    book = {
        "asin": "B012345678",
        "title": "Der Titel",
        "subtitle": "",
        "authors": "Ada Autor",
        "series_title": "",
        "series_sequence": "",
        "narrators": "",
        "release_date": "2025-04-03",
    }
    book.update(overrides)
    service.sync_library_rows([book])
    return book["asin"]


def configured_service(tmp_path, single=None, series=None):
    return AudibleService(
        ServiceConfig(
            config_directory=tmp_path / "config",
            audiobook_root=tmp_path / "audiobooks",
            work_root=tmp_path / "work",
            use_folders=True,
            single_book_path_template=single or "{author}/{title}/{title}.m4b",
            series_book_path_template=series
            or "{author}/{series}/Band {series_index} - {title}/{title}.m4b",
        )
    )


def relative(service, path):
    return path.relative_to(service.config.audiobook_root).as_posix()


def test_standard_path_for_single_book(tmp_path):
    service = configured_service(tmp_path)
    asin = add_book(service)
    assert relative(service, service.render_book_path(asin)) == (
        "Ada Autor/Der Titel/Der Titel.m4b"
    )


def test_standard_path_for_series_book(tmp_path):
    service = configured_service(tmp_path)
    asin = add_book(service, series_title="Meine Serie", series_sequence="1")
    assert relative(service, service.render_book_path(asin)) == (
        "Ada Autor/Meine Serie/Band 1 - Der Titel/Der Titel.m4b"
    )


def test_series_index_is_not_padded(tmp_path):
    service = configured_service(
        tmp_path, series="{series}/Band {series_index}/{title}.m4b"
    )
    asin = add_book(service, series_title="Serie", series_sequence="9")
    assert relative(service, service.render_book_path(asin)) == (
        "Serie/Band 9/Der Titel.m4b"
    )


def test_series_index_padded_rules(tmp_path):
    service = configured_service(
        tmp_path, series="{series}/{series_index_padded}/{title}.m4b"
    )
    for number, expected in (("1", "01"), ("9", "09"), ("10", "10"), ("123", "123"), ("1.5", "1.5")):
        asin = f"B{number.replace('.', '0'):0>9}"[-10:]
        add_book(
            service,
            asin=asin,
            title=f"Titel {number}",
            series_title="Serie",
            series_sequence=number,
        )
        assert relative(service, service.render_book_path(asin)).split("/")[1] == expected


def test_free_text_around_placeholder_is_preserved(tmp_path):
    service = configured_service(
        tmp_path, series="{author}/{series}/Band {series_index_padded} - {title}/{title}.m4b"
    )
    asin = add_book(service, series_title="Serie", series_sequence="3")
    assert "Band 03 - Der Titel" in relative(service, service.render_book_path(asin))


def test_subtitle_placeholder(tmp_path):
    service = configured_service(
        tmp_path, single="{author}/{title} - {subtitle}/{title}.m4b"
    )
    asin = add_book(service, subtitle="Eine Geschichte")
    assert relative(service, service.render_book_path(asin)) == (
        "Ada Autor/Der Titel - Eine Geschichte/Der Titel.m4b"
    )


def test_missing_optional_metadata_does_not_break_rendering(tmp_path):
    service = configured_service(
        tmp_path, single="{author}/{subtitle}/{year}/{title}.m4b"
    )
    asin = add_book(service, subtitle="", narrators="", release_date="")
    assert relative(service, service.render_book_path(asin)) == "Ada Autor/Der Titel.m4b"
