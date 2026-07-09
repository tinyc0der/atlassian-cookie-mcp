# Atlassian access for agents (Jira + Confluence)

**TL;DR for an AI agent:** use the CLI at `./atlassian-cli`. It reuses an existing
Jira/Confluence session from cookies you export once with the **Chrome extension**
(`chrome-extension/`) and load via `atlassian-cli import`. **No browser is ever
opened by this tool.** Read this file, then run the commands below. Do **not**
rebuild or reconfigure anything — it already works once `JIRA_URL` /
`CONFLUENCE_URL` are set and a session is imported.

## How auth resolves (the order — this is the whole design)

1. **Saved cookie jar** still valid → used immediately, no work.
2. **Browser-extension export** — load `chrome-extension/` unpacked, click
   **Export**, then `atlassian-cli import ~/Downloads/atlassian-cookies.json`. The
   extension reads cookies from Chrome's live cookie store (plaintext, incl.
   HttpOnly) via `chrome.cookies.getAll`, so it is immune to the Chrome 127+
   app-bound-encryption problem that blocks reading the cookie DB off disk. See
   [`chrome-extension/README.md`](chrome-extension/README.md).

The MCP **server never opens a window**: on a missing/expired jar it raises
"export cookies and run `atlassian-cli import`" instead of hanging.

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

Re-export and re-`import` whenever the session expires.

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

The saved jar is missing or the session expired. Re-export with the extension
(sign into Jira/Confluence in Chrome first if needed) and import again:

```bash
./atlassian-cli import ~/Downloads/atlassian-cookies.json
```

The `import` command probes the REST API and prints `HTTP 200 (live)` on success,
or a clear "NOT live — re-export" message if the cookies are already expired.

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
