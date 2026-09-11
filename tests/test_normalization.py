import json
import shutil
import subprocess
from pathlib import Path

import pytest
from audible_service import ServiceConfig
from conftest import install_success_pipeline

MEASURED = {
    "input_i": "-23.10",
    "input_tp": "-4.20",
    "input_lra": "5.10",
    "input_thresh": "-33.20",
    "target_offset": "0.10",
}


def test_loudnorm_targets_are_configurable_from_environment(monkeypatch):
    monkeypatch.setenv("AUDIO_NORMALIZE_TARGET_I", "-17")
    monkeypatch.setenv("AUDIO_NORMALIZE_TARGET_TP", "-2")
    monkeypatch.setenv("AUDIO_NORMALIZE_TARGET_LRA", "9")
    config = ServiceConfig.from_environment()
    assert config.normalize_target_i == -17
    assert config.normalize_target_tp == -2
    assert config.normalize_target_lra == 9


def test_normal_conversion_keeps_stream_copy_path(monkeypatch, service):
    source = service._work_directory("B012345678") / "book.aax"
    destination = service.config.audiobook_root / "book.m4b"
    source.write_bytes(b"aax")
    monkeypatch.setattr(service, "_activation_bytes", lambda: "deadbeef")
    commands = []

    def fake_run(command, check=True, **kwargs):
        commands.append(command)
        destination.write_bytes(b"m4b")

    monkeypatch.setattr("audible_service.subprocess.run", fake_run)
    service._convert_file(source, destination)

    assert commands == [[
        "ffmpeg", "-n", "-activation_bytes", "deadbeef", "-i", str(source),
        "-c", "copy", str(destination),
    ]]
    assert "loudnorm" not in " ".join(commands[0])


def test_new_conversion_with_normalization_uses_normalized_branch(monkeypatch, service):
    calls = []

    def download(asin, work_directory):
        source = work_directory / f"{asin}.aax"
        source.write_bytes(b"source")
        return [source]

    def convert(source, destination, **options):
        calls.append(options)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"normalized")
        source.unlink()

    monkeypatch.setattr(service, "_download_audio", download)
    monkeypatch.setattr(service, "_convert_file", convert)
    result = service.process_book("B012345678", normalize=True)

    assert result.successful
    assert calls == [{"overwrite": False, "normalize": True}]
    assert service.book_status("B012345678")["normalization"]["key"] == "normalized"


def _install_transcode_fakes(monkeypatch, service, source, replacement=b"normalized"):
    commands = []
    monkeypatch.setattr(
        service,
        "_probe_audio",
        lambda *args: {
            "stream": {"bit_rate": "96000", "sample_rate": "44100", "channels": 1},
            "streams": [
                {"codec_type": "audio"},
                {"codec_type": "video", "disposition": {"attached_pic": 1}},
            ],
            "format": {},
        },
    )
    monkeypatch.setattr(service, "_measure_loudness", lambda *args: MEASURED)

    def fake_run(command, check=True, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(replacement)

    monkeypatch.setattr("audible_service.subprocess.run", fake_run)
    return commands


def test_existing_m4b_is_normalized_without_download(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    assert service.process_book("B012345678").successful
    normalized = []
    monkeypatch.setattr(
        service,
        "normalize_existing_file",
        lambda path, job_id=None, **kwargs: normalized.append(Path(path)),
    )
    monkeypatch.setattr(
        service, "_download_audio", lambda *args: pytest.fail("must not download")
    )

    queued = service.enqueue_normalize_existing("B012345678")
    assert queued.status == "waiting_for_processing"
    assert service.run_next_download_job() is False
    assert service.run_next_media_job() is True
    assert normalized == [service.config.audiobook_root / service.book_status("B012345678")["outputs"][0]["relative_path"]]


def test_existing_normalization_uses_saved_path_after_template_change(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    saved = service.config.audiobook_root / "old" / "exact.m4b"
    assert service.process_book("B012345678", target_path=saved).successful
    object.__setattr__(service.config, "series_book_path_template", "new/{title}.m4b")
    seen = []
    monkeypatch.setattr(
        service,
        "normalize_existing_file",
        lambda path, job_id=None, **kwargs: seen.append(Path(path)),
    )

    service.enqueue_normalize_existing("B012345678")
    service.run_next_media_job()
    assert seen == [saved]


def test_custom_output_path_is_the_normalization_source(monkeypatch, service):
    install_success_pipeline(monkeypatch, service)
    custom = service.config.audiobook_root / "Eigener Ordner" / "Wunsch.m4b"
    assert service.process_book("B012345678", target_path=custom).successful
    seen = []
    monkeypatch.setattr(
        service,
        "normalize_existing_file",
        lambda path, job_id=None, **kwargs: seen.append(Path(path)),
    )
    service.enqueue_normalize_existing("B012345678")
    service.run_next_media_job()
    assert seen == [custom]


def test_loudnorm_command_preserves_metadata_chapters_cover_and_layout(
    monkeypatch, service
):
    source = service.config.audiobook_root / "source.m4b"
    destination = service.config.audiobook_root / "destination.m4b"
    source.write_bytes(b"original")
    commands = _install_transcode_fakes(monkeypatch, service, source)
    monkeypatch.setattr(service, "_validate_media_output", lambda path: None)

    service._transcode_loudnorm(source, destination)

    command = commands[0]
    mapped = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
    assert mapped == ["0:a", "0:v?", "0:s?"]
    assert command[command.index("-map_metadata") + 1] == "0"
    assert command[command.index("-map_chapters") + 1] == "0"
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-disposition:v:0") + 1] == "attached_pic"
    assert command[command.index("-ar:a") + 1] == "44100"
    assert command[command.index("-ac:a") + 1] == "1"
    assert "measured_I=-23.10" in command[command.index("-af") + 1]


def test_source_is_unchanged_when_ffmpeg_fails(monkeypatch, service):
    source = service.config.audiobook_root / "source.m4b"
    original = b"do-not-damage"
    source.write_bytes(original)
    monkeypatch.setattr(service, "_probe_audio", lambda *args: {"stream": {}, "format": {}})
    monkeypatch.setattr(service, "_measure_loudness", lambda *args: MEASURED)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr("audible_service.subprocess.run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        service.normalize_existing_file(source)

    assert source.read_bytes() == original
    assert list(source.parent.glob(".*.audible-normalize-*.tmp.m4b")) == []


def test_successful_normalization_replaces_only_after_validation(monkeypatch, service):
    source = service.config.audiobook_root / "source.m4b"
    source.write_bytes(b"original")
    _install_transcode_fakes(monkeypatch, service, source, replacement=b"replacement")
    events = []

    def validate(path):
        assert source.read_bytes() == b"original"
        assert path.read_bytes() == b"replacement"
        events.append("validated")

    monkeypatch.setattr(service, "_validate_media_output", validate)
    service.normalize_existing_file(source)

    assert events == ["validated"]
    assert source.read_bytes() == b"replacement"


def test_temporary_file_is_removed_when_validation_fails(monkeypatch, service):
    source = service.config.audiobook_root / "source.m4b"
    source.write_bytes(b"original")
    _install_transcode_fakes(monkeypatch, service, source)
    monkeypatch.setattr(
        service,
        "_validate_media_output",
        lambda path: (_ for _ in ()).throw(RuntimeError("invalid")),
    )
    with pytest.raises(RuntimeError, match="invalid"):
        service.normalize_existing_file(source)
    assert source.read_bytes() == b"original"
    assert list(source.parent.glob(".*.audible-normalize-*.tmp.m4b")) == []


def test_startup_removes_interrupted_normalization_temporary(service_factory):
    service = service_factory()
    temporary = service.config.audiobook_root / ".book.audible-normalize-dead.tmp.m4b"
    unrelated = service.config.audiobook_root / ".book.other.tmp.m4b"
    temporary.write_bytes(b"partial")
    unrelated.write_bytes(b"keep")
    assert service.cleanup_normalization_temporaries() == 1
    assert not temporary.exists()
    assert unrelated.exists()


def test_multipart_normalization_processes_all_parts_in_order(monkeypatch, service):
    install_success_pipeline(monkeypatch, service, parts=3)
    result = service.process_book("B012345678")
    expected = [service.config.audiobook_root / path for path in result.outputs]
    seen = []
    monkeypatch.setattr(
        service,
        "normalize_existing_file",
        lambda path, job_id=None, **kwargs: seen.append(Path(path)),
    )
    service.enqueue_normalize_existing("B012345678")
    service.run_next_media_job()
    assert seen == expected


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg smoke test requires ffmpeg and ffprobe",
)
def test_real_ffmpeg_smoke_preserves_tags_chapter_and_cover(service, tmp_path):
    source = service.config.audiobook_root / "smoke.m4b"
    metadata = tmp_path / "chapters.ffmeta"
    metadata.write_text(
        ";FFMETADATA1\ntitle=Smoke Book\nartist=Smoke Author\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Kapitel 1\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "color=c=blue:s=16x16:d=2", "-i", str(metadata),
            "-map", "0:a", "-map", "1:v", "-map_metadata", "2", "-map_chapters", "2",
            "-c:a", "aac", "-b:a", "64000", "-c:v", "mjpeg", "-frames:v", "1",
            "-disposition:v", "attached_pic", str(source),
        ],
        check=True,
        capture_output=True,
    )

    service.normalize_existing_file(source)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-show_format", "-of", "json", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(probe.stdout)
    assert data["format"]["tags"]["title"] == "Smoke Book"
    assert data["format"]["tags"]["artist"] == "Smoke Author"
    assert len(data["chapters"]) == 1
    assert any(stream.get("disposition", {}).get("attached_pic") for stream in data["streams"])
