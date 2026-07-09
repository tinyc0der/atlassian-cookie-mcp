# Atlassian access for agents (Jira + Confluence)

**TL;DR for an AI agent:** use the CLI at `./atlassian-cli`. It reuses an
existing Jira/Confluence session — first by **auto-harvesting live cookies from a
readable browser** (Arc, Brave, Edge, …), and otherwise from cookies you export
once with the **Chrome extension** (`chrome-extension/`) and load via
`atlassian-cli import`. **No browser is ever opened by this tool.** Read this
file, then run the commands below. Do **not** rebuild or reconfigure anything —
it already works once `JIRA_URL` / `CONFLUENCE_URL` are set and a session is
imported.

## How auth resolves (the order — this is the whole design)

1. **Saved cookie jar** still valid → used immediately, no work.
2. **Auto-harvest** (`cookie_autoauth.py` → `cookie_harvest.py`): scans every
   installed Chromium-family browser, decrypts its Jira/Confluence cookies via
   that browser's macOS Keychain key, and probes the REST API with a **bounded**
   request. The first browser that answers HTTP 200 wins; its cookies are saved
   as the jar. **No window opens, nothing hangs.** Note: **modern Chrome (127+)
   cookies are "app-bound" and cannot be read off disk**, so if your live
   session is only in Chrome, harvest finds nothing — use the extension (below).
3. **Browser-extension export** — the reliable path for Chrome: load
   `chrome-extension/` unpacked, click **Export**, then
   `atlassian-cli import ~/Downloads/atlassian-cookies.json`. The extension reads
   cookies from Chrome's live cookie store (plaintext, incl. HttpOnly), so it is
   immune to the app-bound-encryption problem. See
   [`chrome-extension/README.md`](chrome-extension/README.md).

The MCP **server never opens a window** (it can harvest silently, but on a true
miss it raises "export cookies and run `atlassian-cli import`" instead of
hanging).

**Diagnose first, don't guess:** to see exactly which browsers have readable
cookies and whether any session is live, run `python3 cookie_autoauth.py jira`
(or `confluence`) — it prints a per-browser line like `arc/Default: 7 cookies ->
HTTP 200`. If it says no browser is live (common when your session is only in
modern Chrome), export with the extension and `import`.

## Why this exists

Many Atlassian instances sit behind corporate SSO, which blocks API tokens (and
this also works for Atlassian **Cloud**, `*.atlassian.net`). This tool captures a
real browser session's cookies once and reuses them for headless REST calls.
There are two front-ends over the same auth core (`atlassian_browser_auth.py`):

- **`./atlassian-cli`** — command-line, best for agents. Start here.
- **MCP server** (`atlassian_browser_mcp_full.py`) — for the Cursor/Claude MCP
  integration. Same auth, same session files.

## Configuration

`JIRA_URL` and `CONFLUENCE_URL` are **required** (no hardcoded defaults). Export
them for your instance, e.g.:

```bash
export JIRA_URL="https://yourco.atlassian.net"
export CONFLUENCE_URL="https://yourco.atlassian.net"   # Cloud: /wiki auto-appended
```

On Atlassian Cloud, Jira and Confluence share the tenant host and Confluence REST
lives under `/wiki` — the tool appends `/wiki` automatically for `*.atlassian.net`
Confluence URLs, so the bare tenant host works.

## First-time setup (once per machine)

1. Load the extension: open `chrome://extensions`, enable **Developer mode**,
   click **Load unpacked**, and select `chrome-extension/`.
2. Click the extension icon, enter your Jira and Confluence hosts, click
   **Export** → downloads `atlassian-cookies.json`.
3. Import it (splits into per-service jars and verifies each is live):

   ```bash
   ./atlassian-cli import ~/Downloads/atlassian-cookies.json
   ```

If your live session already lives in Arc/Brave, you can skip the extension and
just run `./atlassian-cli login jira` — it auto-harvests that session. Re-export
and re-`import` whenever the session expires.

## Daily commands (no browser opens)

```bash
# Jira: get an issue (add --comments for the thread, --raw for full JSON)
./atlassian-cli jira get PROJ-123 --comments

# Jira: JQL search
./atlassian-cli jira search 'project = PROJ AND status = "In Progress"' --max 10

# Confluence: get a page as markdown, optionally write to a file
./atlassian-cli confluence get 123456789 --markdown -o /tmp/page.md

# Confluence: search (text or full CQL)
./atlassian-cli confluence search 'release process' --space DEV
```

## If a command says it can't authenticate

First, the tool already tried the saved jar and an auto-harvest and found
nothing live. Confirm what's actually there:

```bash
python3 cookie_autoauth.py jira        # per-browser cookie + live-probe report
```

- If a browser shows `-> HTTP 200`, auth will just work (re-run your command).
- If all show non-200 or "no matching cookies" (typical when your session is only
  in modern Chrome), **re-export with the extension** and import:

```bash
./atlassian-cli import ~/Downloads/atlassian-cookies.json
```

## Files (all git-ignored, local only)

| File | What |
| --- | --- |
| `.atlassian-browser-state-jira.json` | Saved Jira cookie jar |
| `.atlassian-browser-state-confluence.json` | Saved Confluence cookie jar |
| `atlassian-cookies.json` (in ~/Downloads) | Extension export — delete after `import` |

## Env vars

| Var | Default | Purpose |
| --- | --- | --- |
| `JIRA_URL` | (required) | Jira base URL |
| `CONFLUENCE_URL` | (required) | Confluence base URL |
| `ATLASSIAN_COOKIE_HARVEST` | `true` | Master switch for auto-harvest. Set falsy to disable and use only the saved jar / imported cookies. |
| `ATLASSIAN_COOKIE_SOURCE_BROWSERS` | (all installed) | Comma list of browsers to harvest from, in order, e.g. `arc,chrome`. Acts as an allow-list. Known: arc, brave, vivaldi, edge, opera, chrome, chromium, dia. |
| `ATLASSIAN_STORAGE_STATE` | (per-service default) | Override the cookie-jar path; namespaced per service. |
| `ATLASSIAN_BROWSER_USER_AGENT` | Chrome UA | User-Agent used for REST requests and liveness probes. |

## Notes / gotchas

- **Jira Cloud search** uses the enhanced-JQL endpoint (`/rest/api/2/search/jql`);
  it requires a **bounded** query — add a restriction (e.g. `project = X`,
  `updated >= -7d`). An unbounded `order by …` alone returns a clear 400.
- **Confluence Cloud** URLs get `/wiki` appended automatically; Server/DC hosts
  are left as-is.
- The exported `atlassian-cookies.json` holds **live session cookies** — treat it
  like a password and delete it after importing.
- Jira and Confluence keep **separate** cookie jars. On Cloud they share one host,
  so one export covers both; `import` writes both jars.
- Never commit the `.atlassian-browser-*` files or `atlassian-cookies*.json` —
  they contain live session cookies. They are git-ignored.
