import json
import subprocess

import pytest

from usda_cdl import store


def test_load_credentials_from_file(tmp_path):
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "aws_access_key_id": "AKID",
                "aws_secret_access_key": "SECRET",
                "aws_session_token": "TOKEN",
                "region_name": "us-east-1",
            }
        )
    )
    creds = store.load_credentials(str(creds_file))
    assert creds == {"access_key_id": "AKID", "secret_access_key": "SECRET", "session_token": "TOKEN"}


def test_load_credentials_falls_back_to_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(store.shutil, "which", lambda _: "/fake/source-coop")
    monkeypatch.setattr(
        store.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a,
            0,
            stdout=json.dumps(
                {"Version": 1, "AccessKeyId": "CLIKEY", "SecretAccessKey": "CLISECRET", "SessionToken": "CLITOKEN"}
            ),
            stderr="",
        ),
    )
    # missing file -> CLI fallback
    creds = store.load_credentials(str(tmp_path / "absent.json"))
    assert creds["access_key_id"] == "CLIKEY"


def test_load_credentials_cli_not_logged_in(monkeypatch):
    monkeypatch.setattr(store.shutil, "which", lambda _: "/fake/source-coop")
    monkeypatch.setattr(
        store.subprocess,
        "run",
        # the CLI exits 0 even on failure, with a plain-text message
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="Error: No cached credentials found."),
    )
    with pytest.raises(RuntimeError, match="source-coop login"):
        store.load_credentials(None)


def test_load_credentials_no_sources(monkeypatch):
    monkeypatch.setattr(store.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="credentials-file"):
        store.load_credentials(None)
