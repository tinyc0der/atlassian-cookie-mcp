"""Chrome Native Messaging host for one-click cookie sync.

Chrome launches this process (via ``atlassian-native-host``) when the extension
calls ``chrome.runtime.sendNativeMessage``. The host:

1. Loads JIRA_URL / CONFLUENCE_URL from a config file written by
   ``atlassian-cli install-host`` (Chrome does not pass the user's shell env).
2. Reads one length-prefixed JSON message from stdin (cookies from the extension).
3. Runs :func:`atlassian_cookie_import.import_cookies`.
4. Writes a length-prefixed JSON reply and exits.

No browser is opened. No Downloads folder is involved.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from atlassian_cookie_import import import_cookies

# Must match chrome-extension/manifest.json + install-host registration.
NATIVE_HOST_NAME = "com.atlassian_browser_mcp.cookie_host"

# Pinned by the extension manifest "key" field (stable for unpacked loads).
EXTENSION_ID = "eiknaofpjmgjacfiihcmeifjmepobkla"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}/"

# Written by install-host; gitignored. Absolute paths for jars when set.
HOST_ENV_NAME = ".atlassian-native-host-env.json"

# Repo root (this file lives next to the jars / CLI).
PACKAGE_DIR = Path(__file__).resolve().parent


def host_env_path() -> Path:
    return PACKAGE_DIR / HOST_ENV_NAME


def load_host_env(path: Path | None = None) -> dict[str, str]:
    """Load install-host env into os.environ. Returns the loaded mapping."""
    p = path or host_env_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    loaded: dict[str, str] = {}
    for key in (
        "JIRA_URL",
        "CONFLUENCE_URL",
        "ATLASSIAN_STORAGE_STATE",
        "ATLASSIAN_BROWSER_USER_AGENT",
        "ATLASSIAN_SSO_MARKERS",
    ):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            os.environ[key] = val.strip()
            loaded[key] = val.strip()
    return loaded


def write_host_env(
    *,
    jira_url: str,
    confluence_url: str,
    storage_state: str | None = None,
    path: Path | None = None,
) -> Path:
    """Persist env for Chrome-launched host processes."""
    p = path or host_env_path()
    payload: dict[str, str] = {
        "JIRA_URL": jira_url.rstrip("/"),
        "CONFLUENCE_URL": confluence_url.rstrip("/"),
    }
    if storage_state:
        payload["ATLASSIAN_STORAGE_STATE"] = storage_state
    elif "ATLASSIAN_STORAGE_STATE" in os.environ:
        payload["ATLASSIAN_STORAGE_STATE"] = os.environ["ATLASSIAN_STORAGE_STATE"]
    for key in ("ATLASSIAN_BROWSER_USER_AGENT", "ATLASSIAN_SSO_MARKERS"):
        if key in os.environ and os.environ[key].strip():
            payload[key] = os.environ[key].strip()
    p.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def native_host_launcher_path() -> Path:
    """Absolute path to the bash launcher Chrome should execute."""
    return PACKAGE_DIR / "atlassian-native-host"


def host_manifest_dict(host_path: Path | None = None) -> dict[str, Any]:
    path = str((host_path or native_host_launcher_path()).resolve())
    return {
        "name": NATIVE_HOST_NAME,
        "description": "Atlassian browser-mcp cookie import (extension → local jars)",
        "path": path,
        "type": "stdio",
        "allowed_origins": [EXTENSION_ORIGIN],
    }


# Browser id → NativeMessagingHosts directory (per OS). Default install is
# Chrome only; pass browsers=("all",) or a list for others.
def _browser_nm_paths() -> dict[str, Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        return {
            "chrome": base / "Google" / "Chrome" / "NativeMessagingHosts",
            "chrome-canary": base / "Google" / "Chrome Canary" / "NativeMessagingHosts",
            "chromium": base / "Chromium" / "NativeMessagingHosts",
            "brave": base / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts",
            "edge": base / "Microsoft Edge" / "NativeMessagingHosts",
            "vivaldi": base / "Vivaldi" / "NativeMessagingHosts",
            "arc": base / "Arc" / "User Data" / "NativeMessagingHosts",
        }
    if sys.platform.startswith("linux"):
        cfg = home / ".config"
        return {
            "chrome": cfg / "google-chrome" / "NativeMessagingHosts",
            "chromium": cfg / "chromium" / "NativeMessagingHosts",
            "brave": cfg / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts",
            "edge": cfg / "microsoft-edge" / "NativeMessagingHosts",
            "vivaldi": cfg / "vivaldi" / "NativeMessagingHosts",
        }
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        return {
            "chrome": local / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts",
            "edge": local / "Microsoft" / "Edge" / "User Data" / "NativeMessagingHosts",
            "brave": local
            / "BraveSoftware"
            / "Brave-Browser"
            / "User Data"
            / "NativeMessagingHosts",
        }
    return {}


def native_messaging_dirs(
    browsers: list[str] | tuple[str, ...] | None = None,
) -> list[Path]:
    """NativeMessagingHosts directories to register.

    Default is Google Chrome only. Pass ``browsers=["all"]`` or a list of ids
    (``chrome``, ``brave``, ``edge``, …) to include more.
    """
    catalog = _browser_nm_paths()
    if not catalog:
        return []
    if not browsers or browsers == ("chrome",) or browsers == ["chrome"]:
        path = catalog.get("chrome")
        return [path] if path else []
    selected: list[str]
    if "all" in browsers:
        selected = list(catalog.keys())
    else:
        selected = list(browsers)
    out: list[Path] = []
    unknown: list[str] = []
    for name in selected:
        key = name.strip().lower()
        if key not in catalog:
            unknown.append(name)
            continue
        out.append(catalog[key])
    if unknown:
        known = ", ".join(sorted(catalog)) + ", all"
        raise RuntimeError(f"unknown browser(s): {', '.join(unknown)} (known: {known})")
    return out


def install_native_host(
    *,
    jira_url: str | None = None,
    confluence_url: str | None = None,
    host_path: Path | None = None,
    browsers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Write host env + register the host manifest (Chrome only by default)."""
    jira = (jira_url or os.environ.get("JIRA_URL") or "").rstrip("/")
    conf = (confluence_url or os.environ.get("CONFLUENCE_URL") or "").rstrip("/")
    if not jira or not conf:
        raise RuntimeError(
            "JIRA_URL and CONFLUENCE_URL are required to install the native host "
            "(Chrome launches the host without your shell environment)."
        )

    launcher = (host_path or native_host_launcher_path()).resolve()
    if not launcher.is_file():
        raise RuntimeError(f"native host launcher not found: {launcher}")
    if not os.access(launcher, os.X_OK):
        try:
            launcher.chmod(launcher.stat().st_mode | 0o111)
        except OSError as exc:
            raise RuntimeError(f"native host launcher is not executable: {launcher}") from exc

    env_path = write_host_env(jira_url=jira, confluence_url=conf)
    manifest = host_manifest_dict(launcher)
    dirs = native_messaging_dirs(browsers)
    written: list[str] = []
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            dest = d / f"{NATIVE_HOST_NAME}.json"
            dest.write_text(json.dumps(manifest, indent=2) + "\n")
            written.append(str(dest))
        except OSError:
            continue

    if not written:
        raise RuntimeError(
            "could not write any NativeMessagingHosts manifests "
            f"(platform={sys.platform})"
        )

    return {
        "host_name": NATIVE_HOST_NAME,
        "extension_id": EXTENSION_ID,
        "extension_origin": EXTENSION_ORIGIN,
        "launcher": str(launcher),
        "env_file": str(env_path),
        "manifests": written,
        "browsers": browsers or ["chrome"],
    }


# ---- Chrome native-messaging framing (4-byte LE length + UTF-8 JSON) ------

def read_native_message(stdin=None) -> dict[str, Any] | None:
    """Read one native-messaging message. Returns None on clean EOF."""
    inp = stdin or sys.stdin.buffer
    raw_len = inp.read(4)
    if not raw_len:
        return None
    if len(raw_len) < 4:
        raise ValueError("truncated native message length header")
    (length,) = struct.unpack("<I", raw_len)
    # Chrome caps messages at 1 MiB; refuse absurd sizes.
    if length > 1024 * 1024:
        raise ValueError(f"native message too large: {length} bytes")
    body = inp.read(length)
    if len(body) < length:
        raise ValueError("truncated native message body")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("native message must be a JSON object")
    return data


def write_native_message(message: dict[str, Any], stdout=None) -> None:
    out = stdout or sys.stdout.buffer
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise ValueError("native reply too large")
    out.write(struct.pack("<I", len(encoded)))
    out.write(encoded)
    out.flush()


def _hostname_from_url(url: str) -> str | None:
    if not url or not str(url).strip():
        return None
    raw = str(url).strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def configured_product_hosts() -> list[str]:
    """Hostnames from JIRA_URL / CONFLUENCE_URL (env or host-env file)."""
    hosts: list[str] = []
    for key in ("JIRA_URL", "CONFLUENCE_URL"):
        h = _hostname_from_url(os.environ.get(key, ""))
        if h and h not in hosts:
            hosts.append(h)
    return hosts


def hostnames_match(page_host: str, allowed_host: str) -> bool:
    """True if page_host is allowed_host or a subdomain of it (or vice versa)."""
    a = (page_host or "").lower().lstrip(".")
    b = (allowed_host or "").lower().lstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def is_known_atlassian_cloud_host(host: str) -> bool:
    """Well-known Atlassian Cloud product hostnames (no custom DC domains)."""
    h = (host or "").lower().lstrip(".")
    if not h:
        return False
    return (
        h == "atlassian.net"
        or h.endswith(".atlassian.net")
        or h == "jira.com"
        or h.endswith(".jira.com")
    )


def is_allowed_product_host(page_host: str, allowed: list[str] | None = None) -> bool:
    """Whether a browser tab host is treated as Jira/Confluence for Sync.

    Prefer configured JIRA_URL/CONFLUENCE_URL hosts; also accept Atlassian Cloud
    hostnames. Custom Server/DC hosts must appear in the configured list
    (via install-host).
    """
    h = (page_host or "").lower().lstrip(".")
    if not h:
        return False
    allowed = list(allowed) if allowed is not None else configured_product_hosts()
    for a in allowed:
        if hostnames_match(h, a):
            return True
    return is_known_atlassian_cloud_host(h)


def page_host_from_message(msg: dict[str, Any]) -> str | None:
    """Extract page hostname from extension message fields."""
    for key in ("page_host", "host"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower().lstrip(".")
    origin = msg.get("page_origin") or msg.get("origin")
    if isinstance(origin, str) and origin.strip():
        return _hostname_from_url(origin)
    return None


def handle_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one extension message to import_cookies."""
    cmd = msg.get("cmd") or msg.get("command") or "import"
    if cmd not in ("import", "sync", "ping"):
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    allowed = configured_product_hosts()

    if cmd == "ping":
        return {
            "ok": True,
            "host_name": NATIVE_HOST_NAME,
            "extension_id": EXTENSION_ID,
            "env_loaded": host_env_path().is_file(),
            "allowed_hosts": allowed,
        }

    cookies = msg.get("cookies")
    if not isinstance(cookies, list):
        return {"ok": False, "error": "message missing a 'cookies' list"}

    service = msg.get("service")
    if service is not None and service not in ("jira", "confluence"):
        return {"ok": False, "error": f"invalid service: {service}"}

    # Require the tab to be a configured Jira/Confluence (or Cloud) host so a
    # compromised/mis-aimed extension cannot push arbitrary-site cookies.
    page_host = page_host_from_message(msg)
    if not page_host:
        return {
            "ok": False,
            "error": (
                "missing page_host — open a Jira or Confluence tab and Sync again"
            ),
        }
    if not is_allowed_product_host(page_host, allowed):
        hint = (
            f"allowed: {', '.join(allowed)}"
            if allowed
            else "run atlassian-cli install-host with JIRA_URL/CONFLUENCE_URL"
        )
        return {
            "ok": False,
            "error": (
                f"refusing cookies from {page_host!r} — not a Jira/Confluence "
                f"host ({hint})"
            ),
            "page_host": page_host,
            "allowed_hosts": allowed,
        }

    result = import_cookies(cookies, service=service)
    payload = result.to_dict()
    # Never echo cookies back.
    payload["host"] = NATIVE_HOST_NAME
    payload["page_host"] = page_host
    return payload


def main() -> int:
    """Native-messaging entrypoint (stdio). Exit 0 after one reply."""
    # Avoid logging/print noise on stdout — Chrome owns the pipe.
    load_host_env()
    try:
        msg = read_native_message()
        if msg is None:
            write_native_message({"ok": False, "error": "empty message (EOF)"})
            return 1
        reply = handle_message(msg)
        write_native_message(reply)
        return 0 if reply.get("ok") else 2
    except Exception as exc:  # noqa: BLE001 — always answer Chrome
        try:
            write_native_message({"ok": False, "error": str(exc)})
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
