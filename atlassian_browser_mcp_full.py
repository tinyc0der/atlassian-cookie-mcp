#!/usr/bin/env python3
"""Browser-backed wrapper around the upstream mcp-atlassian server."""

from __future__ import annotations

import inspect
import logging
import os
from importlib.metadata import version as package_version
from typing import Any, Literal

import requests
from atlassian import Confluence, Jira
from requests.exceptions import HTTPError

from atlassian_browser_auth import (
    browser_auth_enabled,
    create_browser_session,
)

# Make the upstream server expose its complete tool surface.
os.environ.setdefault("TOOLSETS", "all")
os.environ.setdefault("ATLASSIAN_BROWSER_AUTH_ENABLED", "true")
os.environ.setdefault("JIRA_PERSONAL_TOKEN", "BROWSER_SESSION")
os.environ.setdefault("CONFLUENCE_PERSONAL_TOKEN", "BROWSER_SESSION")

from mcp_atlassian.confluence.client import ConfluenceClient
from mcp_atlassian.confluence.config import ConfluenceConfig
from mcp_atlassian.jira.client import JiraClient
from mcp_atlassian.jira.config import JiraConfig
from mcp_atlassian.jira.forms_api import FormsApiMixin
from mcp_atlassian.jira.forms_common import handle_forms_http_error
from mcp_atlassian.jira.users import UsersMixin
from mcp_atlassian.preprocessing import JiraPreprocessor
from mcp_atlassian.servers.main import main_mcp
from mcp_atlassian.utils.logging import log_config_param
from mcp_atlassian.utils.ssl import configure_ssl_verification

logger = logging.getLogger("atlassian-browser-mcp-full")

_ORIGINAL_JIRA_INIT = JiraClient.__init__
_ORIGINAL_CONFLUENCE_INIT = ConfluenceClient.__init__
_ORIGINAL_LOOKUP_USER_BY_PERMISSIONS = UsersMixin._lookup_user_by_permissions
_ORIGINAL_FORMS_API_REQUEST = FormsApiMixin._make_forms_api_request


def assert_upstream_compatibility() -> None:
    """Verify mcp-atlassian version and patched method signatures are compatible."""
    current_version = package_version("mcp-atlassian")
    if not current_version.startswith("0."):
        raise RuntimeError(
            "This wrapper is pinned for mcp-atlassian 0.x, "
            f"but found {current_version}."
        )

    expected_signatures = [
        ("JiraClient.__init__", _ORIGINAL_JIRA_INIT, ["self", "config"]),
        ("ConfluenceClient.__init__", _ORIGINAL_CONFLUENCE_INIT, ["self", "config"]),
        (
            "UsersMixin._lookup_user_by_permissions",
            _ORIGINAL_LOOKUP_USER_BY_PERMISSIONS,
            ["self", "username"],
        ),
        (
            "FormsApiMixin._make_forms_api_request",
            _ORIGINAL_FORMS_API_REQUEST,
            ["self", "method", "endpoint", "data"],
        ),
    ]
    for label, function, expected_params in expected_signatures:
        actual_params = list(inspect.signature(function).parameters)
        if actual_params[: len(expected_params)] != expected_params:
            raise RuntimeError(
                f"{label} signature changed. "
                f"Expected prefix {expected_params}, got {actual_params}."
            )


def _apply_network_config(
    session: requests.Session,
    config: Any,
    service_name: str,
) -> None:
    """Apply SSL, proxy, and no_proxy settings from config to the session."""
    configure_ssl_verification(
        service_name=service_name,
        url=config.url,
        session=session,
        ssl_verify=config.ssl_verify,
        client_cert=config.client_cert,
        client_key=config.client_key,
        client_key_password=config.client_key_password,
    )

    proxies: dict[str, str] = {}
    if config.http_proxy:
        proxies["http"] = config.http_proxy
    if config.https_proxy:
        proxies["https"] = config.https_proxy
    if config.socks_proxy:
        proxies["socks"] = config.socks_proxy
    if proxies:
        session.proxies.update(proxies)
        for key, value in proxies.items():
            log_config_param(
                logger,
                service_name,
                f"{key.upper()}_PROXY",
                value,
                sensitive=True,
            )
    if config.no_proxy and isinstance(config.no_proxy, str):
        log_config_param(logger, service_name, "NO_PROXY", config.no_proxy)


def _patch_jira_client_init(self: JiraClient, config: Any | None = None) -> None:
    """Replacement __init__ for JiraClient that injects a browser-cookie session."""
    if not browser_auth_enabled():
        _ORIGINAL_JIRA_INIT(self, config)
        return

    self.config = config or JiraConfig.from_env()
    # allow_interactive=False: the MCP server must NEVER open a browser. An
    # interactive login from inside this detached, async-dispatched server is
    # what made tool calls hang forever. On a cookie cache-miss the session
    # raises AuthRequiredError, which surfaces as a clear "run atlassian-cli
    # login jira" message. Authentication is done out-of-band via the CLI.
    session = create_browser_session("jira", self.config.url, allow_interactive=False)
    self.jira = Jira(
        url=self.config.url,
        session=session,
        cloud=self.config.is_cloud,
        verify_ssl=self.config.ssl_verify,
        timeout=self.config.timeout,
    )
    self.jira._session.trust_env = False
    _apply_network_config(self.jira._session, self.config, "Jira")
    if self.config.custom_headers:
        self._apply_custom_headers()

    self.preprocessor = JiraPreprocessor(
        base_url=self.config.url,
        disable_translation=self.config.disable_jira_markup_translation,
    )
    self._field_ids_cache = None
    self._current_user_account_id = None
    self.config.personal_token = None
    self.config.api_token = None
    self.config.username = None


def _patch_confluence_client_init(
    self: ConfluenceClient, config: Any | None = None
) -> None:
    """Replacement __init__ for ConfluenceClient that injects a browser-cookie session."""
    if not browser_auth_enabled():
        _ORIGINAL_CONFLUENCE_INIT(self, config)
        return

    self.config = config or ConfluenceConfig.from_env()
    # allow_interactive=False — see the Jira patch above; the server never
    # opens a browser, it raises AuthRequiredError on a cookie cache-miss.
    session = create_browser_session("confluence", self.config.url, allow_interactive=False)
    self.confluence = Confluence(
        url=self.config.url,
        session=session,
        cloud=self.config.is_cloud,
        verify_ssl=self.config.ssl_verify,
        timeout=self.config.timeout,
    )
    self.confluence._session.trust_env = False
    _apply_network_config(self.confluence._session, self.config, "Confluence")
    if self.config.custom_headers:
        self._apply_custom_headers()

    from mcp_atlassian.preprocessing.confluence import ConfluencePreprocessor

    self.preprocessor = ConfluencePreprocessor(base_url=self.config.url)
    self.config.personal_token = None
    self.config.api_token = None
    self.config.username = None


def _patch_lookup_user_by_permissions(self: UsersMixin, username: str) -> str | None:
    """Look up a user's account ID or name via the permissions search API."""
    if not browser_auth_enabled():
        return _ORIGINAL_LOOKUP_USER_BY_PERMISSIONS(self, username)

    try:
        url = f"{self.config.url}/rest/api/2/user/permission/search"
        response = self.jira._session.get(
            url,
            params={"query": username, "permissions": "BROWSE"},
            verify=self.config.ssl_verify,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        for user in data.get("users", []):
            if self.config.is_cloud and "accountId" in user:
                return user["accountId"]
            if not self.config.is_cloud and "name" in user:
                return user["name"]
            if not self.config.is_cloud and "key" in user:
                return user["key"]
        return None
    except Exception as exc:  # noqa: BLE001
        logger.info("Error looking up user by permissions via browser session: %s", exc)
        return None


def _patch_forms_api_request(
    self: FormsApiMixin,
    method: str,
    endpoint: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a Forms API request using the browser-cookie session."""
    if not browser_auth_enabled():
        return _ORIGINAL_FORMS_API_REQUEST(self, method, endpoint, data)

    if not self._cloud_id:
        raise ValueError(
            "Forms API requires a cloud_id. Provide ATLASSIAN_OAUTH_CLOUD_ID "
            "or X-Atlassian-Cloud-Id when using this tool."
        )

    url = f"https://api.atlassian.com/jira/forms/cloud/{self._cloud_id}{endpoint}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    try:
        response = self.jira._session.request(
            method=method,
            url=url,
            headers=headers,
            json=data,
            timeout=30,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        json_response: dict[str, Any] = response.json()
        return json_response
    except HTTPError as exc:
        logger.error(
            "HTTP error in Forms API (browser auth): %s (status=%s)",
            exc,
            exc.response.status_code if exc.response is not None else "N/A",
        )
        logger.debug(
            "Forms API error response body: %s",
            exc.response.text[:500] if exc.response is not None else "",
        )
        raise handle_forms_http_error(exc, "Forms API request", endpoint) from exc
    except requests.RequestException as exc:
        logger.error("Request error making Forms API request to %s: %s", endpoint, exc)
        raise


JiraClient.__init__ = _patch_jira_client_init
ConfluenceClient.__init__ = _patch_confluence_client_init
UsersMixin._lookup_user_by_permissions = _patch_lookup_user_by_permissions
FormsApiMixin._make_forms_api_request = _patch_forms_api_request


@main_mcp.tool()
def atlassian_login(
    target: Literal["jira", "confluence"] = "jira",
    url: str | None = None,
) -> dict[str, Any]:
    """Report how to authenticate. Cookies are captured OUT-OF-BAND.

    This tool intentionally does NOT open a browser or drive Playwright (removed
    entirely): a sync browser login inside the async-dispatched MCP server
    deadlocks the event loop and hung the server. Instead, capture cookies with
    the Chrome extension (chrome-extension/): prefer **Sync cookies** after
    ``atlassian-cli install-host``, or download JSON and:

        atlassian-cli import ~/Downloads/atlassian-cookies.json

    Once the jar is saved, the server's tools reuse it automatically — no browser
    is ever opened from within the server.
    """
    cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atlassian-cli")
    return {
        "status": "action_required",
        "service": target,
        "message": (
            f"Authentication for {target} is done out-of-band to keep the server "
            f"non-blocking. One-time: `{cli} install-host` (with JIRA_URL/"
            f"CONFLUENCE_URL set). Then click **Sync cookies** in the Chrome "
            f"extension (chrome-extension/), or download JSON and run: "
            f"`{cli} import <atlassian-cookies.json>`. Retry after syncing. "
            f"The server reuses the saved session and never opens a browser itself."
        ),
        "command": f"{cli} install-host  # once; then extension Sync cookies",
        "fallback_command": f"{cli} import <atlassian-cookies.json>",
    }


def main() -> None:
    """Validate environment, check upstream compatibility, and start the MCP server."""
    if not os.environ.get("JIRA_URL"):
        raise RuntimeError("JIRA_URL environment variable is required")
    if not os.environ.get("CONFLUENCE_URL"):
        raise RuntimeError("CONFLUENCE_URL environment variable is required")
    assert_upstream_compatibility()
    main_mcp.run()


if __name__ == "__main__":
    main()
