#!/usr/bin/env python3
"""Shared browser-cookie authentication helpers for Atlassian requests.

Cookies are captured OUT-OF-BAND (the Chrome extension in ``chrome-extension/``
exports them; ``atlassian-cli import`` loads them) or auto-harvested from a live
browser session (``cookie_autoauth``). Nothing here ever opens a browser window
or drives Playwright — on a cache miss it raises :class:`AuthRequiredError`.
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
            f"Not authenticated for {service}. Export cookies with the Atlassian "
            f"Cookie Exporter browser extension, then run "
            f"`atlassian-cli import <file>` to load them (or sign into {service} "
            f"in Arc/Brave and retry to auto-harvest a live session). The server "
            f"never opens a browser itself."
        )


# Serializes cookie refresh/harvest across threads so concurrent 401s don't
# stampede the harvest + jar rewrite.
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


class BrowserCookieSession(requests.Session):
    """Requests session backed by cookies captured out-of-band.

    Cookies come from the saved storage-state jar (written by
    ``atlassian-cli import`` from the browser-extension export) or from an
    auto-harvested live browser session. This session NEVER opens a browser: on
    a cache miss or auth failure it raises :class:`AuthRequiredError`.
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
            # No saved session and nothing to harvest: re-raise so the caller
            # (e.g. the MCP server) gets an immediate, clear "log in" signal
            # instead of a half-built session that fails opaquely on first use.
            raise
        except Exception as exc:
            logger.debug("Cookie loading failed for %s", service, exc_info=True)
            print(
                f"[atlassian-browser-auth] Could not load browser cookies for {service}: {exc}. "
                "Export cookies with the browser extension and run `atlassian-cli import`.",
                file=sys.stderr,
                flush=True,
            )

    def refresh_cookies(self) -> None:
        """Reload cookies from the saved jar, harvesting from a live browser if needed.

        Order of preference:
          1. A valid saved storage-state jar (fast path, no work).
          2. Auto-harvest: reuse a LIVE session from any installed browser
             (Arc/Brave/...) whose cookies are still readable off disk. This
             opens no window and is bounded.
        No browser window is ever opened. On a miss, raises AuthRequiredError so
        the caller tells the user to export cookies with the extension and run
        `atlassian-cli import`.
        """
        if not self.browser_config.storage_state.exists():
            # Try harvesting a live session from any browser first (no UI, bounded).
            if self._try_auto_harvest():
                storage_state = _load_storage_state(self.browser_config.storage_state)
                _apply_storage_state_cookies(self, storage_state, self.base_url)
                return
            # Nothing on disk and nothing to harvest: fail fast with a clear
            # instruction instead of blocking the caller.
            raise AuthRequiredError(self.service)
        storage_state = _load_storage_state(self.browser_config.storage_state)
        _apply_storage_state_cookies(self, storage_state, self.base_url)

    def _try_auto_harvest(self) -> bool:
        """Reuse a live session from any installed browser; write the jar if found.

        Returns True iff a browser yielded an authenticated (HTTP 200) session
        and its cookies were written to this service's storage-state path. Opens
        no browser window and is fully bounded, so it is safe in server mode.
        Controlled by ATLASSIAN_COOKIE_HARVEST (default on); set falsy to skip.
        """
        if not _env_truthy("ATLASSIAN_COOKIE_HARVEST", True):
            return False
        try:
            # Imported lazily so a harvest-disabled or non-macOS environment never
            # pays the import cost (and a missing optional dep can't break auth).
            from cookie_autoauth import auto_harvest
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-harvest unavailable: %s", exc)
            return False
        try:
            res = auto_harvest(
                self.service,
                self.base_url,
                storage_state_path=self.browser_config.storage_state,
                user_agent=self.browser_config.user_agent,
            )
        except Exception as exc:  # noqa: BLE001 - harvest must never break auth
            logger.debug("auto-harvest error for %s: %s", self.service, exc)
            return False
        if res.authenticated:
            print(
                f"[atlassian-browser-auth] Reused live {self.service} session from "
                f"{res.browser}/{res.profile} ({res.cookie_count} cookies) — no login needed.",
                file=sys.stderr,
                flush=True,
            )
            return True
        if res.attempts:
            print(
                f"[atlassian-browser-auth] No live {self.service} session in any browser "
                f"({'; '.join(res.attempts)}).",
                file=sys.stderr,
                flush=True,
            )
        return False

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Make a request, re-loading cookies once on an SSO redirect or 401.

        On a re-auth signal we NEVER open a browser. We reload the saved jar (a
        separate `atlassian-cli import` may have refreshed it), retry, and if
        still stale try harvesting a live session from another browser. If
        nothing authenticates, raise AuthRequiredError so the caller can tell the
        user to re-export cookies with the extension.
        """
        retry_on_auth = kwargs.pop("_retry_on_auth", True)
        response = super().request(method, url, *args, **kwargs)
        needs_reauth = looks_like_sso_response(response) or response.status_code == 401
        if not (retry_on_auth and needs_reauth):
            return response

        response.close()
        with _LOGIN_LOCK:
            # Reload from disk; refresh_cookies raises AuthRequiredError if the
            # jar is absent and nothing can be harvested.
            self.refresh_cookies()
            retest = super().request(method, url, *args, **kwargs)
            if looks_like_sso_response(retest) or retest.status_code == 401:
                # Disk jar is also stale. Try harvesting a fresh live session
                # from another browser (bounded, no UI) before giving up — this
                # is how we self-heal when the user re-authed in their normal
                # browser.
                retest.close()
                if self._try_auto_harvest():
                    storage_state = _load_storage_state(
                        self.browser_config.storage_state
                    )
                    _apply_storage_state_cookies(self, storage_state, self.base_url)
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
    saved/harvested cookies and raises AuthRequiredError on a cache miss.
    """
    return BrowserCookieSession(
        service=service,
        base_url=base_url,
        config=config,
        allow_interactive=allow_interactive,
    )
