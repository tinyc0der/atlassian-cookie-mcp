"""Tests for `atlassian-cli import` — loading an extension cookie export.

These prove the import command's decision logic without any network: we set
JIRA_URL/CONFLUENCE_URL to synthetic hosts, redirect the cookie jars into a temp
dir via ATLASSIAN_STORAGE_STATE, and stub the liveness probe. Coverage:

  - cookies split into the correct per-service jar by domain (incl. parent-domain
    cookies shared by both, and IdP cookies routed to neither)
  - session cookies (expires:-1) survive into the jar unchanged
  - --service limits the write to a single jar
  - malformed JSON / missing cookies list are rejected (exit 2)
  - an export with no matching cookies exits 2
  - a NOT-live import still writes the jar but exits 2
  - successful import deletes the export JSON (credentials must not linger)
  - failed import (no match / bad file) leaves the export in place
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import atlassian_cli as cli
import atlassian_cookie_import as cookie_import


def _write_export(tmp_path: Path, cookies: list[dict]) -> Path:
    p = tmp_path / "atlassian-cookies.json"
    p.write_text(json.dumps({"cookies": cookies, "origins": []}))
    return p


def _jar(tmp_path: Path, svc: str) -> dict:
    return json.loads((tmp_path / f"state-{svc}.json").read_text())


def _names(jar: dict) -> set[str]:
    return {c["name"] for c in jar["cookies"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Synthetic hosts + temp jars + a probe that reports 'live' (HTTP 200)."""
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.com")
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cookie_import, "probe_live", lambda *a, **k: 200)
    return tmp_path


def test_import_splits_by_domain(env, tmp_path):
    export = _write_export(tmp_path, [
        {"name": "JSESSIONID", "value": "j", "domain": "jira.example.com",
         "path": "/", "secure": True, "httpOnly": True, "expires": -1},
        {"name": "shared", "value": "s", "domain": ".example.com",
         "path": "/", "secure": True, "httpOnly": False, "expires": 1900000000},
        {"name": "confSess", "value": "c", "domain": "confluence.example.com",
         "path": "/", "secure": True, "httpOnly": True, "expires": -1},
        {"name": "idp", "value": "x", "domain": "okta.com",
         "path": "/", "secure": True, "httpOnly": True, "expires": -1},
    ])

    cli.cmd_import(SimpleNamespace(file=str(export), service=None))

    assert _names(_jar(tmp_path, "jira")) == {"JSESSIONID", "shared"}
    assert _names(_jar(tmp_path, "confluence")) == {"confSess", "shared"}
    # The IdP cookie belongs to neither service jar.
    assert "idp" not in _names(_jar(tmp_path, "jira"))
    assert "idp" not in _names(_jar(tmp_path, "confluence"))
    # Export is deleted after jars are written (treat like a password).
    assert not export.exists()


def test_session_cookie_expires_minus_one_survives(env, tmp_path):
    export = _write_export(tmp_path, [
        {"name": "JSESSIONID", "value": "j", "domain": "jira.example.com",
         "path": "/", "secure": True, "httpOnly": True, "expires": -1},
    ])
    cli.cmd_import(SimpleNamespace(file=str(export), service="jira"))
    sess = next(c for c in _jar(tmp_path, "jira")["cookies"] if c["name"] == "JSESSIONID")
    assert sess["expires"] == -1
    assert not export.exists()


def test_service_filter_only_writes_that_jar(env, tmp_path):
    export = _write_export(tmp_path, [
        {"name": "JSESSIONID", "value": "j", "domain": "jira.example.com", "path": "/", "expires": -1},
        {"name": "confSess", "value": "c", "domain": "confluence.example.com", "path": "/", "expires": -1},
    ])
    cli.cmd_import(SimpleNamespace(file=str(export), service="jira"))
    assert (tmp_path / "state-jira.json").exists()
    assert not (tmp_path / "state-confluence.json").exists()
    assert not export.exists()


def test_malformed_json_rejected(env, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(SystemExit) as ei:
        cli.cmd_import(SimpleNamespace(file=str(bad), service=None))
    assert ei.value.code == 2
    assert bad.exists()  # never consumed — leave for the user to fix


def test_missing_cookies_list_rejected(env, tmp_path):
    bad = tmp_path / "nolist.json"
    bad.write_text(json.dumps({"origins": []}))
    with pytest.raises(SystemExit) as ei:
        cli.cmd_import(SimpleNamespace(file=str(bad), service=None))
    assert ei.value.code == 2
    assert bad.exists()


def test_no_matching_cookies_exits_2(env, tmp_path):
    export = _write_export(tmp_path, [
        {"name": "idp", "value": "x", "domain": "okta.com", "path": "/", "expires": -1},
    ])
    with pytest.raises(SystemExit) as ei:
        cli.cmd_import(SimpleNamespace(file=str(export), service=None))
    assert ei.value.code == 2
    assert export.exists()  # not imported — keep so env/host can be fixed and retried


def test_not_live_exits_2_but_writes_jar(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.com")
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cookie_import, "probe_live", lambda *a, **k: 401)
    export = _write_export(tmp_path, [
        {"name": "JSESSIONID", "value": "j", "domain": "jira.example.com", "path": "/", "expires": -1},
    ])
    with pytest.raises(SystemExit) as ei:
        cli.cmd_import(SimpleNamespace(file=str(export), service="jira"))
    assert ei.value.code == 2
    # Jar is written even when not live, so a later refresh/retry can reuse it.
    assert (tmp_path / "state-jira.json").exists()
    # Export still deleted: cookies were consumed into the jar (and are dead either way).
    assert not export.exists()
