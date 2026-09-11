import sys
from pathlib import Path

import pytest

APP_DIRECTORY = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIRECTORY))

from audible_service import AudibleService, ServiceConfig


@pytest.fixture
def service_factory(tmp_path):
    def create(**overrides):
        defaults = {
            "config_directory": tmp_path / "config",
            "audiobook_root": tmp_path / "audiobooks",
            "work_root": tmp_path / "work",
            "use_folders": True,
        }
        defaults.update(overrides)
        return AudibleService(ServiceConfig(**defaults))

    return create


@pytest.fixture
def service(service_factory):
    instance = service_factory()
    instance.sync_library_rows(
        [
            {
                "asin": "B012345678",
                "title": "Der Titel",
                "subtitle": "Der Untertitel",
                "authors": "Ada Autor",
                "series_title": "Meine Serie",
                "series_sequence": "1",
                "narrators": "Nora Erzählerin",
                "release_date": "2025-04-03",
            }
        ]
    )
    return instance


def install_success_pipeline(monkeypatch, service, parts=1):
    calls = []

    def download(asin, work_directory):
        sources = []
        for number in range(1, parts + 1):
            source = work_directory / f"{asin}_part{number}.aax"
            source.write_bytes(b"aax")
            sources.append(source)
        calls.append(("download", asin))
        return sources

    def convert(source, destination, overwrite=False):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"converted:{source.name}".encode())
        source.unlink()
        calls.append(("convert", destination, overwrite))

    def merge(parts_to_merge, destination, book, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"".join(path.read_bytes() for path in parts_to_merge))
        calls.append(("merge", destination, len(parts_to_merge)))

    monkeypatch.setattr(service, "_download_audio", download)
    monkeypatch.setattr(service, "_convert_file", convert)
    monkeypatch.setattr(service, "_merge_m4b_parts", merge)
    return calls
