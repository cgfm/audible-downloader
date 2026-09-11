import subprocess

import pytest


def test_ffmpeg_progress_uses_media_timestamp(monkeypatch, service):
    seen_commands = []

    class FakeProcess:
        stdout = iter(
            [
                "out_time_us=2500000\n",
                "progress=continue\n",
                "out_time_us=7500000\n",
                "progress=end\n",
            ]
        )

        def wait(self):
            return 0

    def popen(command, **options):
        seen_commands.append((command, options))
        return FakeProcess()

    monkeypatch.setattr("audible_service.subprocess.Popen", popen)
    progress = []

    result = service._run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "input.m4b", "output.m4b"],
        10,
        progress.append,
    )

    command, options = seen_commands[0]
    assert command[:4] == ["ffmpeg", "-progress", "pipe:1", "-nostats"]
    assert options == {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
    }
    assert progress == [0.25, 0.75, 1.0]
    assert result.returncode == 0


def test_conversion_job_persists_monotonic_progress(monkeypatch, service):
    def download(asin, work_directory, progress_callback=None):
        progress_callback(0.4)
        progress_callback(1.0)
        source = work_directory / f"{asin}.aax"
        source.write_bytes(b"audio")
        return [source]

    observed = []

    def convert(source, destination, progress_callback=None, **kwargs):
        progress_callback(0.25)
        progress_callback(0.75)
        with service._connect() as connection:
            observed.append(
                connection.execute(
                    "SELECT progress_percent, phase_progress_percent FROM jobs"
                ).fetchone()
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"converted")
        source.unlink()

    monkeypatch.setattr(service, "_download_audio", download)
    monkeypatch.setattr(service, "_convert_file", convert)
    queued = service.enqueue_conversion("B012345678")

    assert service.run_next_download_job()
    waiting_job = service.list_jobs()[0]
    assert waiting_job["progress_percent"] == 30
    assert waiting_job["phase_label"] == "Download (aktuelle Datei)"
    assert waiting_job["phase_progress_percent"] == 100
    assert service.run_next_media_job()

    job = next(job for job in service.list_jobs() if job["id"] == queued.job_id)
    assert [tuple(row) for row in observed] == [(82, 75)]
    assert job["status"] == "completed"
    assert job["progress_percent"] == 100
    assert job["phase_label"] == "FFmpeg-Medienverarbeitung"
    assert job["phase_progress_percent"] == 100


def test_multipart_progress_is_weighted_by_media_duration(monkeypatch, service):
    work = service.config.work_root / "multipart-progress"
    work.mkdir()
    sources = [work / "part-1.aax", work / "part-2.aax"]
    for source in sources:
        source.write_bytes(b"audio")
    durations = {sources[0]: 10.0, sources[1]: 20.0}

    monkeypatch.setattr(service, "_activation_bytes", lambda: "deadbeef")
    monkeypatch.setattr(
        service,
        "_probe_audio",
        lambda source, input_options=None: {
            "stream": {"duration": str(durations[source])},
            "format": {"duration": str(durations[source])},
        },
    )

    def convert(source, destination, progress_callback=None, **kwargs):
        destination.write_bytes(b"converted")
        progress_callback(0.5)
        progress_callback(1.0)

    def merge(parts, destination, book, progress_callback=None, **kwargs):
        destination.write_bytes(b"merged")
        progress_callback(0.5)
        progress_callback(1.0)

    monkeypatch.setattr(service, "_convert_file", convert)
    monkeypatch.setattr(service, "_merge_m4b_parts", merge)
    progress = []

    service._convert_downloaded_files(
        sources,
        service.config.audiobook_root / "merged.m4b",
        service.get_book("B012345678"),
        overwrite=False,
        normalize=False,
        progress_callback=progress.append,
    )

    assert progress == pytest.approx([1 / 18, 1 / 9, 2 / 9, 1 / 3, 2 / 3, 1])
