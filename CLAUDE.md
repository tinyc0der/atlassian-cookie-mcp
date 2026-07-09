# CLAUDE.md - atlassian-browser-mcp

## Project Overview

MCP server wrapping upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) with browser-cookie authentication. Cookies are captured out-of-band — exported from real Chrome by a bundled Chrome extension (`chrome-extension/`) and loaded via `atlassian-cli import` — so it works for Atlassian **Cloud** (`*.atlassian.net`) and Server/Data Center instances behind corporate SSO where API tokens are unavailable. **No Playwright, no browser automation.**

## Architecture

- `atlassian_browser_mcp_full.py` — MCP entrypoint. Monkey-patches upstream `JiraClient` and `ConfluenceClient` constructors to inject browser-cookie sessions. Registers `atlassian_login` tool (returns instructions only). Runs the MCP server.
- `atlassian_browser_auth.py` — Shared auth core: `BrowserCookieSession` (requests.Session subclass), saved-jar loading, `write_storage_state`/`probe_live` helpers, SSO redirect detection. **Never opens a browser.** Both the MCP server and the CLI use this.
- `atlassian_cli.py` + `atlassian-cli` — Command-line front-end over the same auth core (`install-host`, `import`, Jira/Confluence get/search). Preferred for agents and scripting; no MCP transport involved. See `AGENT_USAGE.md`.
- `atlassian_cookie_import.py` — Shared import + probe used by CLI and native host.
- `atlassian_native_host.py` + `atlassian-native-host` — Chrome Native Messaging host for one-click extension **Sync** (no Downloads).
- `chrome-extension/` — Manifest V3 Chrome extension. Reads live cookies via `chrome.cookies.getAll` and Syncs them via the native host (or downloads JSON for `import`). The only way to seed cookies (Chrome 127+ app-bound cookies can't be read off disk, and no browser is ever driven).
- `run-atlassian-browser-mcp.sh` — MCP launcher: creates venv via `uv`, installs deps, runs upstream compatibility check, starts server.

## ⚠️ The server NEVER opens a browser (nothing here does)

Capturing cookies and serving REST calls are **separate jobs**, and **no part of
this tool opens a browser or drives Playwright** — Playwright has been removed
entirely.

- **Capturing cookies is out-of-band**: the user Syncs cookies with the Chrome
  extension (`chrome-extension/`) via the native host (`install-host`), or
  downloads JSON and runs `atlassian-cli import`. Interactive login is not
  something this tool performs.
- **Serving is fast, stateless HTTP**: `BrowserCookieSession` reads the saved
  cookie jar and, on a cache miss or 401, raises `AuthRequiredError`
  **immediately** ("export cookies and run `atlassian-cli import`") instead of
  blocking.

**Why this exists (the bug it fixes):** the server used to call
`interactive_login()` (Playwright) from inside its detached, async-dispatched
process. That (a) blocked the tool call waiting for a human who couldn't see the
window, and (b) ran `sync_playwright()` on the asyncio event loop, which
deadlocks ("Playwright Sync API inside the asyncio loop"). Net effect: MCP
Atlassian calls hung forever. Removing Playwright and moving capture to the
extension eliminates that hang class entirely.

**Hard rules — do not regress:**
- No code path (server or CLI) may drive Playwright or open a browser. The
  `atlassian_login` tool returns instructions only.
- Any new session must resolve cookies from the saved jar (seeded by
  `atlassian-cli import`), never by launching a UI.
- `allow_interactive` on `BrowserCookieSession` / `create_browser_session` is
  retained for API compatibility only; it no longer gates a browser launch
  (there is none).

## ⚠️ Authentication: reuse the real session via the extension (do not re-enter user/pw)

Re-entering corporate username/password + MFA on every run must be avoided; the
whole point is a **persistent, reusable session**. How it stays reliable:

1. **Sync from real Chrome with the extension.** `chrome-extension/` reads the
   live cookie store via `chrome.cookies.getAll` (plaintext, incl. HttpOnly), so
   it reuses whatever SSO session Chrome already has — no password, no MFA
   re-prompt. Preferred path: **Sync** → native host → jars (after
   `install-host`). Fallback: download JSON + `import`. This is the supported
   path because **Chrome 127+ "app-bound" cookies cannot be decrypted off disk**.
2. **NEVER delete the cookie jars on an auth failure.** On re-auth, only the
   short-lived per-service cache (`.atlassian-browser-state-<svc>.json`) is
   rewritten from a fresh sync/import; nothing long-lived is wiped.
3. **Native host env is separate from the shell.** Chrome launches the host with
   a clean environment; `install-host` freezes `JIRA_URL`/`CONFLUENCE_URL` into
   `.atlassian-native-host-env.json` (gitignored). Re-run `install-host` if those
   URLs change.

Jira and Confluence keep **separate cookie jars** (`*-state-jira.json`,
`*-state-confluence.json`). On Atlassian Cloud they share one host, so a single
extension Sync covers both. All `.atlassian-browser-*` artifacts,
`.atlassian-native-host-env.json`, and `atlassian-cookies*.json` exports hold
live credentials and are git-ignored — never commit them.

Operational how-to (commands, env vars) lives in [`AGENT_USAGE.md`](AGENT_USAGE.md);
this section is the rationale and the hard rules.

## Key Design Decisions

- **Monkey-patching over forking**: We patch upstream client `__init__` methods at import time rather than maintaining a fork. An `assert_upstream_compatibility()` check validates signatures on startup.
- **Version pinning**: Pinned to `mcp-atlassian>=0.21.1,<1.0.0`. Bump carefully — upstream signature changes will break patches.
- **No hardcoded URLs**: `JIRA_URL` and `CONFLUENCE_URL` are required env vars with no defaults.
- **Configurable SSO detection**: `ATLASSIAN_SSO_MARKERS` env var accepts comma-separated markers. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML.

## Development Guidelines

- Keep browser auth logic in `atlassian_browser_auth.py`, MCP/patching logic in `atlassian_browser_mcp_full.py`.
- When upstream `mcp-atlassian` releases a new minor version, update the pin in `pyproject.toml` and verify `assert_upstream_compatibility()` passes.
- Never store credentials, URLs, or company-specific references in source code.

*Generated by [LynxPrompt](https://lynxprompt.com) CLI*
