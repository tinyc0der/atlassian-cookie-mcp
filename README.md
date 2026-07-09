<p align="center">
  <img src="docs/images/banner.svg" alt="atlassian-browser-mcp banner" width="900"/>
</p>

# atlassian-browser-mcp

[![License: GPL-3.0](https://img.shields.io/github/license/GeiserX/atlassian-browser-mcp?style=flat-square)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![GitHub stars](https://img.shields.io/github/stars/GeiserX/atlassian-browser-mcp?style=flat-square)](https://github.com/GeiserX/atlassian-browser-mcp/stargazers)
[![mcp-atlassian](https://img.shields.io/badge/wraps-mcp--atlassian%200.x-blue?style=flat-square)](https://github.com/sooperset/mcp-atlassian)
[![GeiserX/atlassian-browser-mcp MCP server](https://glama.ai/mcp/servers/GeiserX/atlassian-browser-mcp/badges/score.svg)](https://glama.ai/mcp/servers/GeiserX/atlassian-browser-mcp)

MCP server that wraps the upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) toolset with browser-cookie authentication. Cookies are captured from your real Chrome by a bundled **Chrome extension** — **no Playwright, no browser automation.** Works for Atlassian **Cloud** (`*.atlassian.net`) and Server/Data Center behind corporate SSO (Okta, SAML, etc.) where API tokens are not available.

## How it works

Capturing cookies and serving data are **separate**, and there is **no browser automation** anywhere — this is what keeps the MCP server from hanging:

1. **Capture cookies with the Chrome extension.** Load `chrome-extension/` unpacked. After a one-time `atlassian-cli install-host`, click **Sync cookies** — the extension hands cookies to a local Native Messaging host that writes per-service jars (no Downloads). Fallback: **Download JSON only** + `atlassian-cli import`.
2. **The MCP server serves data only.** It reads the saved cookies via a custom `requests.Session` subclass and never opens a browser. On a missing/expired session it fails fast with an `AuthRequiredError` telling you to re-sync — it does **not** block.

> ⚠️ Earlier versions launched a Playwright login browser from inside the server. Because the server is detached and async, that blocked tool calls for minutes (often forever) and could deadlock Playwright's sync API on the event loop. Moving capture to the extension removes that failure mode — and removes the need to read Chrome's on-disk cookie DB, which Chrome 127+ "app-bound" encryption blocks.

The server monkey-patches `JiraClient` and `ConfluenceClient` constructors in `mcp-atlassian` to inject the browser-cookie session, giving full parity with the upstream tool surface.

## Files

| File | Purpose |
|------|---------|
| `atlassian_browser_mcp_full.py` | MCP entrypoint. Patches upstream clients, registers `atlassian_login` tool, runs the MCP server |
| `atlassian_browser_auth.py` | Shared auth core: `BrowserCookieSession`, saved-jar loading, `write_storage_state`/`probe_live`, SSO detection. Never opens a browser |
| `atlassian_cli.py` + `atlassian-cli` | Command-line front-end (`install-host`, `import`, Jira/Confluence get/search). See [`AGENT_USAGE.md`](AGENT_USAGE.md) |
| `atlassian_cookie_import.py` | Shared cookie → jar import + liveness probe (CLI and native host) |
| `atlassian_native_host.py` + `atlassian-native-host` | Chrome Native Messaging host for one-click Sync |
| `chrome-extension/` | Manifest V3 extension: Sync via native host, or download JSON — see [`chrome-extension/README.md`](chrome-extension/README.md) |
| `run-atlassian-browser-mcp.sh` | MCP launcher: creates venv, installs deps via `uv`, runs compatibility check, starts server |
| `pyproject.toml` | Dependency pins |

## Reusing your real Chrome session (the Chrome extension)

Modern Chrome (127+) encrypts cookies with an app-bound key that can't be read
off disk, so the reliable way to reuse your Chrome SSO session is the bundled
extension — it reads cookies from Chrome's live cookie store, no password or MFA
re-prompt:

1. `export JIRA_URL=… CONFLUENCE_URL=…` then `./atlassian-cli install-host`
   (registers the native host; freezes URLs for Chrome-launched processes).
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
   select `chrome-extension/` → reload after install-host.
3. Click the extension, enter hosts, click **Sync cookies**.

Cookie jars are **never auto-deleted** on an auth failure. Jira and Confluence
keep separate jars; on Atlassian Cloud they share one host, so a single sync
covers both. See [`chrome-extension/README.md`](chrome-extension/README.md) for
details and the managed-Chrome caveat.

## CLI usage

```bash
export JIRA_URL="https://yourco.atlassian.net"
export CONFLUENCE_URL="https://yourco.atlassian.net"   # Cloud: same host

./atlassian-cli install-host                               # once per machine
# then: extension → Sync cookies
# fallback:
./atlassian-cli import ~/Downloads/atlassian-cookies.json

./atlassian-cli jira get PROJ-123 --comments
./atlassian-cli jira search 'project = PROJ AND status = "In Progress"'
./atlassian-cli confluence get 123456789 --markdown -o page.md
./atlassian-cli confluence search 'release process' --space DEV
```

## Usage

```bash
./run-atlassian-browser-mcp.sh
```

### MCP server configuration

Add to your Claude Code, Cursor, or other MCP client configuration:

```json
{
  "mcpServers": {
    "atlassian": {
      "command": "/path/to/atlassian-browser-mcp/run-atlassian-browser-mcp.sh",
      "env": {
        "JIRA_URL": "https://yourco.atlassian.net",
        "CONFLUENCE_URL": "https://yourco.atlassian.net"
      }
    }
  }
}
```

The server never opens a browser. Capture cookies once with the extension Sync
(or `import`); all MCP tool calls then proceed using the saved session, and a
missing/expired session fails fast with a clear re-sync message.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_URL` | _(required)_ | Jira base URL (e.g. `https://yourco.atlassian.net`) |
| `CONFLUENCE_URL` | _(required)_ | Confluence base URL (e.g. `https://yourco.atlassian.net`). For Cloud (`*.atlassian.net`) the `/wiki` context path is appended automatically |
| `ATLASSIAN_BROWSER_AUTH_ENABLED` | `true` | Enable browser-cookie auth (set `false` to fall back to token auth) |
| `ATLASSIAN_STORAGE_STATE` | `./.atlassian-browser-state-{service}.json` | Cookie-jar file. Per-service by default; an explicit value is still namespaced per service |
| `ATLASSIAN_SSO_MARKERS` | _(auto)_ | Comma-separated URL/text markers for SSO redirect detection. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML |
| `ATLASSIAN_BROWSER_USER_AGENT` | _(Chrome 136)_ | Custom User-Agent string for API requests and liveness probes |
| `TOOLSETS` | `all` | Which upstream toolsets to enable |

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (for dependency management)
- Google Chrome (or another Chromium-family browser) to run the extension and capture the session
- Network access to your Atlassian instance

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Tools return `AuthRequiredError` / "not authenticated" | No saved session (jar missing or expired) | Extension **Sync** (after `install-host`), or `atlassian-cli import <file>` |
| `import` reports HTTP 401/302 (not live) | Exported cookies are already expired | Sign into Jira/Confluence in Chrome, re-**Export**, and `import` again |
| No cookies match your hosts on `import` | `JIRA_URL`/`CONFLUENCE_URL` don't match the exported cookies' domain | Fix the env vars to point at the same instance you exported from |
| "Load unpacked" is greyed out | Managed/corporate Chrome blocks unpacked extensions | Ask IT to allowlist the extension, or pack & self-host it (see `chrome-extension/README.md`) |
| "Upstream compatibility check failed" | `mcp-atlassian` version changed its internal API | Pin to a compatible version or update the wrapper |


