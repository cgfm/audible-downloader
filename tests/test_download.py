import subprocess
import sys


def test_download_uses_existing_work_directory_and_one_internal_job(
    monkeypatch, service
):
    monkeypatch.setattr(service, "_activation_bytes", lambda: "deadbeef")
    commands = []

    def download(command, **options):
        commands.append((command, options))
        output = service.config.work_root / "nested" / "B012345678_audio.aax"
        assert output.parent.is_dir()
        output.write_bytes(b"audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("audible_service.subprocess.run", download)

    outputs = service._download_audio(
        "B012345678", service.config.work_root / "nested"
    )

    command, options = commands[0]
    assert outputs == [service.config.work_root / "nested" / "B012345678_audio.aax"]
    assert command[command.index("--jobs") + 1] == "1"
    assert "--no-progress" in command
    assert "--no-confirm" in command
    assert options == {"check": True, "capture_output": True, "text": True}


def test_download_failure_includes_audible_stderr_and_is_persisted(
    monkeypatch, service
):
    monkeypatch.setattr(service, "_activation_bytes", lambda: "deadbeef")

    def fail(command, progress_callback):
        raise subprocess.CalledProcessError(
            2,
            command,
            stderr="Asin B012345678 not found in library.",
        )

    monkeypatch.setattr(service, "_run_audible_download_with_progress", fail)
    queued = service.enqueue_conversion("B012345678")

    assert service.run_next_download_job() is True
    job = next(job for job in service.list_jobs() if job["id"] == queued.job_id)
    assert job["status"] == "failed"
    assert "Exit 2" in job["error_message"]
    assert "Asin B012345678 not found in library." in job["error_message"]


def test_machine_download_progress_uses_transferred_bytes(monkeypatch, service):
    seen_commands = []

    class FakeProcess:
        stdout = iter(
            [
                "AUDIBLE_DOWNLOAD_PROGRESS=25/100\n",
                "AUDIBLE_DOWNLOAD_PROGRESS=75/100\n",
                "ordinary output\n",
                "AUDIBLE_DOWNLOAD_PROGRESS=100/100\n",
            ]
        )

        def wait(self):
            return 0

    def popen(command, **options):
        seen_commands.append((command, options))
        return FakeProcess()

    monkeypatch.setattr("audible_service.subprocess.Popen", popen)
    progress = []

    result = service._run_audible_download_with_progress(
        ["audible", "download", "-a", "B012345678"], progress.append
    )

    command, options = seen_commands[0]
    assert command[0] == sys.executable
    assert command[1].endswith("audible_progress.py")
    assert command[2:] == ["download", "-a", "B012345678"]
    assert options == {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
    }
    assert progress == [0.25, 0.75, 1.0]
    assert result.stdout == "ordinary output\n"
