import json
import subprocess
import time
import tomllib
from pathlib import Path

import pytest


class FakeAuthenticator:
    def to_file(self, filename, **kwargs):
        Path(filename).write_text(
            json.dumps(
                {
                    "adp_token": "new-adp-token",
                    "device_private_key": "new-private-key",
                    "refresh_token": "new-refresh-token",
                    "with_username": False,
                    "locale_code": "de",
                }
            ),
            encoding="utf-8",
        )


def write_library(path: Path, title="Web-Anmeldung"):
    path.write_text(
        "asin\ttitle\tauthors\n"
        f"B088888888\t{title}\tEine Autorin\n",
        encoding="utf-8",
    )


def wait_for_flow(service, flow_id, *states):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        flow = service.authentication_flow(flow_id)
        if flow and flow["status"] in states:
            return flow
        time.sleep(0.01)
    raise AssertionError(f"authentication flow did not reach {states}")


def install_successful_authentication(monkeypatch, service):
    def create_authenticator(country_code, with_username, callback):
        assert country_code == "de"
        assert with_username is False
        callback_url = callback("https://www.amazon.de/ap/signin?client=test")
        assert "openid.oa2.authorization_code=success" in callback_url
        return FakeAuthenticator()

    def export_library(config_directory, output_directory):
        assert (config_directory / "config.toml").is_file()
        with (config_directory / "config.toml").open("rb") as file:
            config = tomllib.load(file)
        auth_filename = config["profile"]["audible"]["auth_file"]
        assert auth_filename.startswith("audible-auth-")
        assert (config_directory / auth_filename).is_file()
        library_path = output_directory / "library.tsv"
        write_library(library_path)
        return library_path

    monkeypatch.setattr(service, "_create_external_authenticator", create_authenticator)
    monkeypatch.setattr(service, "_export_audible_library", export_library)


def test_browser_authentication_installs_profile_after_validation(monkeypatch, service):
    install_successful_authentication(monkeypatch, service)

    started = service.start_authentication("de")

    assert started["status"] == "waiting_for_login"
    assert started["login_url"].startswith("https://www.amazon.de/")
    assert not (service.config.config_directory / "audible.json").exists()

    service.submit_authentication_callback(
        started["flow_id"],
        "https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=success",
    )
    completed = wait_for_flow(service, started["flow_id"], "completed")

    assert completed["login_url"] is None
    assert service.audible_authentication_status()["connected"] is True
    auth_filename = service.audible_authentication_status()["auth_file"]
    assert auth_filename.startswith("audible-auth-")
    assert (service.config.config_directory / auth_filename).stat().st_mode & 0o777 == 0o600
    assert service.get_book("B088888888")["title"] == "Web-Anmeldung"
    config = (service.config.config_directory / "config.toml").read_text(
        encoding="utf-8"
    )
    assert 'primary_profile = "audible"' in config
    assert 'country_code = "de"' in config


def test_failed_reauthentication_preserves_existing_auth(monkeypatch, service):
    config_directory = service.config.config_directory
    (config_directory / "config.toml").write_text(
        'title = "Audible Config File"\n'
        "[APP]\n"
        'primary_profile = "audible"\n'
        "[profile.audible]\n"
        'auth_file = "audible.json"\n'
        'country_code = "de"\n',
        encoding="utf-8",
    )
    old_auth = json.dumps(
        {"adp_token": "old-token", "device_private_key": "old-key"}
    ).encode()
    (config_directory / "audible.json").write_bytes(old_auth)

    def create_authenticator(country_code, with_username, callback):
        callback("https://www.amazon.de/ap/signin?client=test")
        return FakeAuthenticator()

    def failing_export(config_directory, output_directory):
        raise subprocess.CalledProcessError(
            1, ["audible", "library", "export"], output="sensitive authorization code"
        )

    monkeypatch.setattr(
        service, "_create_external_authenticator", create_authenticator
    )
    monkeypatch.setattr(service, "_export_audible_library", failing_export)

    started = service.start_authentication("de")
    service.submit_authentication_callback(
        started["flow_id"],
        "https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=secret",
    )
    failed = wait_for_flow(service, started["flow_id"], "failed")

    assert "sensitive" not in failed["error"]
    assert (config_directory / "audible.json").read_bytes() == old_auth
    assert service.audible_authentication_status()["connected"] is True


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://example.com/ap/maplanding?openid.oa2.authorization_code=code",
        "http://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=code",
        "https://www.amazon.de/not-maplanding?openid.oa2.authorization_code=code",
        "https://www.amazon.de/ap/maplanding",
        "https://www.amazon.de:8443/ap/maplanding?openid.oa2.authorization_code=code",
    ],
)
def test_authentication_rejects_untrusted_callback_urls(
    monkeypatch, service, callback_url
):
    install_successful_authentication(monkeypatch, service)
    started = service.start_authentication("de")

    with pytest.raises(ValueError):
        service.submit_authentication_callback(started["flow_id"], callback_url)

    assert service.authentication_flow(started["flow_id"])["status"] == (
        "waiting_for_login"
    )
    assert service.cancel_authentication(started["flow_id"]) is True


def test_structural_authentication_status_reports_encrypted_file(service):
    config_directory = service.config.config_directory
    (config_directory / "config.toml").write_text(
        "[APP]\n"
        'primary_profile = "audible"\n'
        "[profile.audible]\n"
        'auth_file = "audible.json"\n'
        'country_code = "de"\n',
        encoding="utf-8",
    )
    (config_directory / "audible.json").write_text(
        json.dumps({"ciphertext": "encrypted"}), encoding="utf-8"
    )

    status = service.audible_authentication_status()

    assert status["connected"] is False
    assert status["state"] == "invalid"
    assert "verschlüsselt" in status["error"]


def test_startup_removes_orphaned_auth_staging_and_managed_file(service_factory):
    service = service_factory()
    staging = service.config.config_directory / ".audible-auth-interrupted"
    staging.mkdir()
    (staging / "credentials.json").write_text(
        '{"access_token": "STAGING-SECRET"}', encoding="utf-8"
    )
    orphan = service.config.config_directory / "audible-auth-orphan.json"
    orphan.write_text('{"access_token": "ORPHAN-SECRET"}', encoding="utf-8")

    service_factory()

    assert not staging.exists()
    assert not orphan.exists()
