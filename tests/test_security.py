import logging
import subprocess

from conftest import install_success_pipeline
from web import create_app


def test_activation_bytes_output_is_captured_and_secret_is_registered(
    monkeypatch, service
):
    marker = "ACTIVATION-MARKER-7d54f32a"
    auth_file = service.config.config_directory / "audible.json"
    auth_file.write_text(f'{{"activation_bytes": "{marker}"}}', encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=f"{marker}\n")

    monkeypatch.setattr("audible_service.subprocess.run", run)

    assert service._activation_bytes() == marker
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.STDOUT
    assert marker not in service.public_error_message(RuntimeError(marker))


def test_subprocess_secrets_never_reach_log_database_or_web(
    monkeypatch, service, caplog
):
    activation = "ACTIVATION-MARKER-a8b4691c"
    authorization_code = "AUTH-CODE-MARKER-bad764af"
    service._register_secret(activation)
    install_success_pipeline(monkeypatch, service)

    def fail_conversion(*args, **kwargs):
        raise subprocess.CalledProcessError(
            9,
            ["ffmpeg", "-activation_bytes", activation, "input.aax"],
            output=(
                "failed callback=https://www.amazon.de/ap/maplanding?"
                f"openid.oa2.authorization_code={authorization_code}"
            ),
        )

    monkeypatch.setattr(service, "_convert_file", fail_conversion)
    queued = service.enqueue_conversion("B012345678")
    with caplog.at_level(logging.ERROR):
        service.run_next_download_job()
        service.run_next_media_job()

    with service._connect() as connection:
        job_error = connection.execute(
            "SELECT error_message FROM jobs WHERE id=?", (queued.job_id,)
        ).fetchone()[0]
        run_error = connection.execute(
            "SELECT error_message FROM processing_runs WHERE job_id=?",
            (queued.job_id,),
        ).fetchone()[0]

    app = create_app(service)
    app.config.update(TESTING=True)
    client = app.test_client()
    responses = (client.get("/jobs").data, client.get("/").data)
    exposed = "\n".join(
        [caplog.text, job_error, run_error]
        + [response.decode("utf-8") for response in responses]
    )
    assert activation not in exposed
    assert authorization_code not in exposed
    assert "[REDACTED]" in exposed


def test_startup_scrubs_legacy_persisted_command_secrets(service_factory):
    service = service_factory()
    service.sync_library_rows(
        [{"asin": "B000000009", "title": "Alt", "authors": "Autor"}]
    )
    run_id = service._create_run(
        "B000000009",
        service.default_target_path("B000000009"),
        False,
        False,
        True,
        "test",
    )
    marker = "LEGACY-ACTIVATION-MARKER"
    with service._connect() as connection:
        connection.execute(
            "UPDATE processing_runs SET error_message=? WHERE id=?",
            (f"ffmpeg -activation_bytes {marker} failed", run_id),
        )

    restarted = service_factory()
    with restarted._connect() as connection:
        stored = connection.execute(
            "SELECT error_message FROM processing_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
    assert marker not in stored
    assert "[REDACTED]" in stored
