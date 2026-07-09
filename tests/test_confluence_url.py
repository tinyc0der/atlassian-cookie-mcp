"""Tests for Cloud Confluence /wiki URL normalization.

Atlassian Cloud serves Confluence REST under /wiki; a CONFLUENCE_URL without it
404s every call. `_normalize_confluence_url` appends /wiki for *.atlassian.net
hosts only, leaving Server/DC hosts untouched.
"""

from __future__ import annotations

import pytest

from atlassian_browser_auth import BrowserAuthConfig


@pytest.mark.parametrize("inp,expected", [
    # Cloud: /wiki appended
    ("https://x.atlassian.net", "https://x.atlassian.net/wiki"),
    ("https://x.atlassian.net/", "https://x.atlassian.net/wiki"),
    # Cloud: already present -> unchanged
    ("https://x.atlassian.net/wiki", "https://x.atlassian.net/wiki"),
    ("https://x.atlassian.net/wiki/", "https://x.atlassian.net/wiki/"),
    # Server/DC: not *.atlassian.net -> untouched
    ("https://confluence.example.com", "https://confluence.example.com"),
    ("https://confluence.example.com/confluence", "https://confluence.example.com/confluence"),
    ("", ""),
])
def test_normalize_confluence_url(inp, expected):
    assert BrowserAuthConfig._normalize_confluence_url(inp) == expected


def test_from_env_appends_wiki(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://x.atlassian.net")
    monkeypatch.setenv("CONFLUENCE_URL", "https://x.atlassian.net")
    cfg = BrowserAuthConfig.from_env("confluence")
    assert cfg.confluence_url == "https://x.atlassian.net/wiki"
    assert cfg.service_base("confluence") == "https://x.atlassian.net/wiki"
    # Jira host is never touched.
    assert cfg.service_base("jira") == "https://x.atlassian.net"


def test_from_env_leaves_server_dc_confluence(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.com")
    cfg = BrowserAuthConfig.from_env("confluence")
    assert cfg.confluence_url == "https://confluence.example.com"
