"""Tests for Chrome native-messaging host framing and install helpers."""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import atlassian_native_host as host


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def test_read_write_native_message_roundtrip():
    payload = {"cmd": "ping", "n": 1}
    buf_in = io.BytesIO(_frame(payload))
    msg = host.read_native_message(buf_in)
    assert msg == payload

    buf_out = io.BytesIO()
    host.write_native_message({"ok": True, "pong": True}, buf_out)
    buf_out.seek(0)
    assert host.read_native_message(buf_out) == {"ok": True, "pong": True}


def test_read_native_message_eof():
    assert host.read_native_message(io.BytesIO(b"")) is None


def test_handle_ping():
    reply = host.handle_message({"cmd": "ping"})
    assert reply["ok"] is True
    assert reply["host_name"] == host.NATIVE_HOST_NAME
    assert reply["extension_id"] == host.EXTENSION_ID


def test_handle_import_missing_cookies():
    reply = host.handle_message({"cmd": "import"})
    assert reply["ok"] is False
    assert "cookies" in reply["error"]


def test_handle_import_delegates(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.com")
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(
        "atlassian_cookie_import.probe_live", lambda *a, **k: 200
    )

    cookies = [
        {
            "name": "JSESSIONID",
            "value": "j",
            "domain": "jira.example.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": -1,
        }
    ]
    reply = host.handle_message({"cmd": "import", "cookies": cookies, "service": "jira"})
    assert reply["ok"] is True
    assert reply["any_live"] is True
    assert reply["services"]["jira"]["status"] == 200
    assert (tmp_path / "state-jira.json").exists()
    # Never echo secrets.
    assert "JSESSIONID" not in json.dumps(reply)
    assert "value" not in json.dumps(reply) or '"value"' not in json.dumps(
        {k: v for k, v in reply.items() if k != "services"}
    )


def test_write_and_load_host_env(tmp_path, monkeypatch):
    path = tmp_path / "env.json"
    monkeypatch.delenv("JIRA_URL", raising=False)
    host.write_host_env(
        jira_url="https://jira.example.com",
        confluence_url="https://conf.example.com",
        path=path,
    )
    assert path.exists()
    loaded = host.load_host_env(path)
    assert loaded["JIRA_URL"] == "https://jira.example.com"
    assert loaded["CONFLUENCE_URL"] == "https://conf.example.com"


def test_native_messaging_dirs_default_chrome_only(monkeypatch):
    chrome = Path("/tmp/chrome-nm")
    brave = Path("/tmp/brave-nm")
    monkeypatch.setattr(
        host,
        "_browser_nm_paths",
        lambda: {"chrome": chrome, "brave": brave},
    )
    assert host.native_messaging_dirs() == [chrome]
    assert host.native_messaging_dirs(None) == [chrome]
    assert host.native_messaging_dirs(["chrome"]) == [chrome]
    assert host.native_messaging_dirs(["all"]) == [chrome, brave]
    assert host.native_messaging_dirs(["brave"]) == [brave]
    with pytest.raises(RuntimeError, match="unknown browser"):
        host.native_messaging_dirs(["netscape"])


def test_install_native_host(tmp_path, monkeypatch):
    launcher = tmp_path / "atlassian-native-host"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)

    env_path = tmp_path / "host-env.json"
    nm_dir = tmp_path / "Chrome" / "NativeMessagingHosts"
    monkeypatch.setattr(host, "host_env_path", lambda: env_path)
    monkeypatch.setattr(
        host,
        "_browser_nm_paths",
        lambda: {"chrome": nm_dir, "brave": tmp_path / "Brave" / "NativeMessagingHosts"},
    )
    monkeypatch.setattr(host, "native_host_launcher_path", lambda: launcher)

    info = host.install_native_host(
        jira_url="https://jira.example.com",
        confluence_url="https://conf.example.com",
    )
    assert info["host_name"] == host.NATIVE_HOST_NAME
    assert info["extension_id"] == host.EXTENSION_ID
    assert info["browsers"] == ["chrome"]
    assert env_path.is_file()
    assert len(info["manifests"]) == 1
    manifest_path = Path(info["manifests"][0])
    assert manifest_path.is_file()
    assert str(nm_dir) in str(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["name"] == host.NATIVE_HOST_NAME
    assert manifest["path"] == str(launcher.resolve())
    assert host.EXTENSION_ORIGIN in manifest["allowed_origins"]
    # Other browsers not touched by default.
    assert not (tmp_path / "Brave" / "NativeMessagingHosts").exists()


def test_install_host_requires_urls(monkeypatch, tmp_path):
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("CONFLUENCE_URL", raising=False)
    launcher = tmp_path / "host"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(host, "native_host_launcher_path", lambda: launcher)
    with pytest.raises(RuntimeError, match="JIRA_URL"):
        host.install_native_host()


def test_cmd_install_host_cli(monkeypatch, tmp_path, capsys):
    import atlassian_cli as cli

    launcher = tmp_path / "atlassian-native-host"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    env_path = tmp_path / "env.json"
    nm_dir = tmp_path / "nm"
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("CONFLUENCE_URL", "https://conf.example.com")
    monkeypatch.setattr(host, "host_env_path", lambda: env_path)
    monkeypatch.setattr(
        host, "_browser_nm_paths", lambda: {"chrome": nm_dir}
    )
    monkeypatch.setattr(host, "native_host_launcher_path", lambda: launcher)
    # CLI imports install_native_host at module level — patch there too.
    monkeypatch.setattr(cli, "install_native_host", host.install_native_host)

    cli.cmd_install_host(
        SimpleNamespace(json=False, all_browsers=False, browsers=None)
    )
    out = capsys.readouterr().out
    assert host.NATIVE_HOST_NAME in out
    assert host.EXTENSION_ID in out
    assert env_path.is_file()
