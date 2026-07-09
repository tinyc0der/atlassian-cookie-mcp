#!/usr/bin/env python3
"""Shared browser-cookie authentication helpers for Atlassian requests.

Cookies are captured OUT-OF-BAND: the Chrome extension in ``chrome-extension/``
exports them and ``atlassian-cli import`` loads them into the jar this module
reads. Nothing here ever opens a browser window or drives Playwright — on a
cache miss it raises :class:`AuthRequiredError`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests

logger = logging.getLogger("atlassian-browser-auth")

ServiceName = Literal["jira", "confluence"]


class AuthRequiredError(RuntimeError):
    """Raised when a session has no valid cookies.

    The caller (e.g. the MCP server) should surface this to the user as a clear
    instruction to authenticate out-of-band, instead of blocking on an
    interactive login. Carries the service so the message names the exact
    command to run.
    """

    def __init__(self, service: ServiceName) -> None:
        self.service = service
        super().__init__(
            f"Not authenticated for {service}. Open a Jira/Confluence tab and "
            f"Sync cookies with the browser extension (after "
            f"`atlassian-cli install-host`). The server never opens a browser itself."
        )


# Serializes cookie reloads across threads so concurrent 401s don't stampede the
# jar re-read.
_LOGIN_LOCK = threading.Lock()

_DEFAULT_SSO_MARKERS = (
    "oauth2/authorize",
    "The page has timed out",
    "Sign in with your account",
    "saml2/idp/SSOService",
    "/adfs/ls",
    "login.microsoftonline.com",
    "accounts.google.com/o/saml2",
    "auth.pingone.com",
    "login.okta.com",
)


def _env_truthy(name: str, default: bool) -> bool:
    """Check if an environment variable holds a truthy value."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def browser_auth_enabled() -> bool:
    """Return whether browser-based authentication is enabled."""
    return _env_truthy("ATLASSIAN_BROWSER_AUTH_ENABLED", True)


def _default_port(parsed) -> int:
    """Return the effective port for a parsed URL, defaulting by scheme."""
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def url_matches_base(current_url: str, base_url: str) -> bool:
    """Check if current_url belongs to the same origin as base_url."""
    current = urlparse(current_url)
    base = urlparse(base_url)
    return (
        current.scheme == base.scheme
        and current.hostname == base.hostname
        and _default_port(current) == _default_port(base)
    )


@dataclass(frozen=True)
class BrowserAuthConfig:
    """Configuration for browser-cookie Atlassian authentication."""

    jira_url: str
    confluence_url: str
    storage_state: Path
    user_agent: str

    @classmethod
    def from_env(cls, service: "ServiceName | None" = None) -> "BrowserAuthConfig":
        """Build configuration from environment variables.

        When ``service`` is given, the storage-state cookie cache is namespaced
        per service (Jira vs Confluence) so their cookies do not overwrite each
        other. Passing no service preserves the legacy single state file for
        backward compatibility.
        """
        jira_url = os.environ.get("JIRA_URL", "").rstrip("/")
        confluence_url = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
        if not jira_url:
            raise RuntimeError(
                "JIRA_URL environment variable is required. "
                "Set it to your Jira instance URL (e.g., https://jira.example.com)"
            )
        if not confluence_url:
            raise RuntimeError(
                "CONFLUENCE_URL environment variable is required. "
                "Set it to your Confluence instance URL (e.g., https://confluence.example.com)"
            )
        confluence_url = cls._normalize_confluence_url(confluence_url)
        base_dir = Path(__file__).resolve().parent
        return cls(
            jira_url=jira_url,
            confluence_url=confluence_url,
            storage_state=cls._resolve_storage_state(base_dir, service),
            user_agent=os.environ.get(
                "ATLASSIAN_BROWSER_USER_AGENT",
                (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            ),
        )

    @staticmethod
    def _resolve_storage_state(base_dir: Path, service: "ServiceName | None") -> Path:
        """Resolve the cookie-jar path, per service, with legacy adoption.

        - Explicit ATLASSIAN_STORAGE_STATE always wins; when a service is given
          we still namespace it (insert ``-{service}`` before the suffix) so the
          two services never share one jar and overwrite each other's cookies.
        - Otherwise default to ``.atlassian-browser-state-{service}.json`` (or
          the legacy single name when no service is given).
        - One-time migration: if the per-service file does not exist yet but the
          legacy ``.atlassian-browser-state.json`` does, adopt it (copy) so an
          already-authenticated user is NOT forced into a surprise re-login on
          upgrade. Cookies are per-profile, so reusing the legacy jar is sound.
        """
        override = os.environ.get("ATLASSIAN_STORAGE_STATE")
        if override:
            p = Path(override).expanduser()
            if service:
                p = p.with_name(f"{p.stem}-{service}{p.suffix}")
            return p
        if not service:
            return (base_dir / ".atlassian-browser-state.json").expanduser()
        path = (base_dir / f".atlassian-browser-state-{service}.json").expanduser()
        legacy = base_dir / ".atlassian-browser-state.json"
        if not path.exists() and legacy.exists():
            try:
                shutil.copy2(legacy, path)
                path.chmod(0o600)
                print(
                    f"[atlassian-browser-auth] Adopted legacy session for {service} "
                    "(no re-login needed).",
                    file=sys.stderr,
                    flush=True,
                )
            except OSError:
                pass
        return path

    @staticmethod
    def _normalize_confluence_url(url: str) -> str:
        """Ensure Atlassian Cloud Confluence URLs include the /wiki context path.

        On Cloud, Confluence REST lives under
        ``https://<tenant>.atlassian.net/wiki``. A CONFLUENCE_URL without /wiki
        makes every Confluence REST call 404, so append it when the host is
        ``*.atlassian.net`` and the path doesn't already include it. Server/DC
        hosts (not ``*.atlassian.net``) are left untouched.
        """
        if not url:
            return url
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.endswith(".atlassian.net") and "/wiki" not in parsed.path:
            return url.rstrip("/") + "/wiki"
        return url

    def service_base(self, service: ServiceName) -> str:
        """Return the base URL for the given service."""
        return self.jira_url if service == "jira" else self.confluence_url


def _load_storage_state(path: Path) -> dict[str, Any]:
    """Load and validate the storage state JSON file (Playwright-compatible)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Browser storage state does not exist yet: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt storage state file %s, removing: %s", path, exc)
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Browser storage state was corrupt and has been removed: {path}"
        ) from exc

    if not isinstance(data.get("cookies"), list):
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Invalid storage state structure in {path} (missing cookies list)"
        )
    return data


def _cookie_matches_base_url(cookie: dict[str, Any], base_url: str) -> bool:
    """Check if a cookie's domain matches the target base URL."""
    hostname = urlparse(base_url).hostname or ""
    domain = (cookie.get("domain") or "").lstrip(".")
    if not domain or domain.count(".") < 1:
        return False
    return hostname == domain or hostname.endswith(f".{domain}")


def _apply_storage_state_cookies(
    session: requests.Session,
    storage_state: dict[str, Any],
    base_url: str,
) -> None:
    """Apply cookies from storage state to a requests session."""
    session.cookies.clear()
    now = time.time()
    for cookie in storage_state.get("cookies", []):
        if not _cookie_matches_base_url(cookie, base_url):
            continue
        expires = cookie.get("expires")
        if expires and expires not in (-1, 0) and float(expires) < now:
            continue
        rest: dict[str, Any] = {}
        if cookie.get("httpOnly") is not None:
            rest["HttpOnly"] = cookie.get("httpOnly")
        if cookie.get("sameSite"):
            rest["SameSite"] = cookie.get("sameSite")
        session.cookies.set(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
            secure=bool(cookie.get("secure")),
            expires=None
            if expires in (None, -1, 0)
            else int(float(expires)),
            rest=rest,
        )


def _load_sso_markers() -> tuple[str, ...]:
    """Load SSO detection markers from env or use sensible defaults."""
    custom = os.environ.get("ATLASSIAN_SSO_MARKERS")
    if custom:
        markers = tuple(m.strip() for m in custom.split(",") if len(m.strip()) >= 3)
        if markers:
            return markers
        logger.warning(
            "ATLASSIAN_SSO_MARKERS is set but contains no valid markers (min 3 chars); "
            "falling back to defaults"
        )
    return _DEFAULT_SSO_MARKERS


def looks_like_sso_response(response: requests.Response) -> bool:
    """Detect whether an HTTP response is an SSO/login redirect."""
    final_url = response.url or ""
    content_type = response.headers.get("Content-Type", "")
    is_html = "text/html" in content_type or "xhtml" in content_type
    body_sample = (
        response.content[:2000].decode(errors="ignore")
        if is_html
        else ""
    )
    markers = _load_sso_markers()
    url_markers = [m for m in markers if "/" in m or "." in m]
    if any(marker in final_url for marker in url_markers):
        return True
    if any(
        any(marker in prior.url for marker in url_markers)
        for prior in response.history
    ):
        return True
    return is_html and any(marker in body_sample for marker in markers)


def write_storage_state(cookies: list[dict[str, Any]], path: Path) -> None:
    """Persist cookies as a storage_state JSON (mode 0600), written atomically."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


# REST endpoints that return 200 only for an authenticated session and, when
# logged out, 302-redirect to SSO (which, with redirects disabled, surfaces as a
# non-200 — so only a genuine live session passes).
_VERIFY_PATHS: dict[str, str] = {
    "jira": "/rest/api/2/myself",
    "confluence": "/rest/api/space?limit=1",
}
# Bounded (connect, read) so a dead endpoint can't hang the caller.
_PROBE_TIMEOUT = (5, 8)


def probe_live(
    base_url: str,
    service: ServiceName,
    cookies: list[dict[str, Any]],
    user_agent: str,
) -> int:
    """Return the HTTP status of a bounded, redirect-free liveness probe.

    0 means the request could not be completed (timeout / connection error).
    Never raises. Used by ``atlassian-cli import`` to confirm the cookies it just
    wrote actually authenticate.
    """
    path = _VERIFY_PATHS[service]
    sess = requests.Session()
    sess.trust_env = False  # ignore ambient proxy env that could hijack the probe
    sess.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    for c in cookies:
        try:
            sess.cookies.set(
                c["name"],
                c["value"],
                domain=str(c.get("domain", "")).lstrip("."),
                path=c.get("path", "/"),
            )
        except Exception:  # noqa: BLE001 - a malformed cookie shouldn't kill the probe
            continue
    try:
        resp = sess.get(f"{base_url}{path}", allow_redirects=False, timeout=_PROBE_TIMEOUT)
        return resp.status_code
    except requests.RequestException:
        return 0
    finally:
        sess.close()


class BrowserCookieSession(requests.Session):
    """Requests session backed by cookies captured out-of-band.

    Cookies come from the saved storage-state jar, written by ``atlassian-cli
    import`` from the browser-extension export. This session NEVER opens a
    browser: on a cache miss or auth failure it raises :class:`AuthRequiredError`.
    """

    def __init__(
        self,
        service: ServiceName,
        base_url: str,
        config: BrowserAuthConfig | None = None,
        allow_interactive: bool = True,
    ) -> None:
        super().__init__()
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.browser_config = config or BrowserAuthConfig.from_env(service)
        # Retained for API compatibility (the MCP server passes False). It no
        # longer gates a browser launch — NO code path here opens a browser or
        # drives Playwright anymore; login happens out-of-band via the extension
        # + `atlassian-cli import`. Kept so existing callers need no change.
        self.allow_interactive = allow_interactive
        self.trust_env = False
        self.headers.update({
            "User-Agent": self.browser_config.user_agent,
            # Jira/Confluence Data Center reject cookie-authenticated mutating
            # requests (POST/PUT/DELETE) as XSRF unless both this header AND a
            # same-origin Origin header are present. Origin is the load-bearing
            # one; X-Atlassian-Token alone is not sufficient.
            "X-Atlassian-Token": "no-check",
            "Origin": self.base_url,
        })
        try:
            self.refresh_cookies()
        except AuthRequiredError:
            # No saved session: re-raise so the caller (e.g. the MCP server) gets
            # an immediate, clear "log in" signal instead of a half-built session
            # that fails opaquely on first use.
            raise
        except Exception as exc:
            logger.debug("Cookie loading failed for %s", service, exc_info=True)
            print(
                f"[atlassian-browser-auth] Could not load browser cookies for {service}: {exc}. "
                "Sync cookies with the browser extension (or `atlassian-cli import`).",
                file=sys.stderr,
                flush=True,
            )

    def refresh_cookies(self) -> None:
        """Reload cookies from the saved storage-state jar.

        No browser window is ever opened. On a missing jar, raises
        AuthRequiredError so the caller tells the user to export cookies with the
        extension and run `atlassian-cli import`.
        """
        if not self.browser_config.storage_state.exists():
            raise AuthRequiredError(self.service)
        storage_state = _load_storage_state(self.browser_config.storage_state)
        _apply_storage_state_cookies(self, storage_state, self.base_url)

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Make a request, re-loading cookies once on an SSO redirect or 401.

        On a re-auth signal we NEVER open a browser. We reload the saved jar (a
        separate `atlassian-cli import` may have refreshed it) and retry once. If
        it still doesn't authenticate, raise AuthRequiredError so the caller can
        tell the user to re-sync cookies with the extension.
        """
        retry_on_auth = kwargs.pop("_retry_on_auth", True)
        response = super().request(method, url, *args, **kwargs)
        needs_reauth = looks_like_sso_response(response) or response.status_code == 401
        if not (retry_on_auth and needs_reauth):
            return response

        response.close()
        with _LOGIN_LOCK:
            # Reload from disk; refresh_cookies raises AuthRequiredError if the
            # jar is absent.
            self.refresh_cookies()
            retest = super().request(method, url, *args, **kwargs)
        if not looks_like_sso_response(retest) and retest.status_code != 401:
            return retest
        retest.close()
        raise AuthRequiredError(self.service)


def create_browser_session(
    service: ServiceName,
    base_url: str,
    config: BrowserAuthConfig | None = None,
    allow_interactive: bool = True,
) -> BrowserCookieSession:
    """Create a BrowserCookieSession for the given Atlassian service.

    ``allow_interactive`` is retained for API compatibility (the MCP server
    passes False); no code path opens a browser regardless. The session reads
    the saved cookie jar and raises AuthRequiredError on a cache miss.
    """
    return BrowserCookieSession(
        service=service,
        base_url=base_url,
        config=config,
        allow_interactive=allow_interactive,
    )
