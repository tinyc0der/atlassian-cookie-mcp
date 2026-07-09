# Atlassian Cookie Exporter (Chrome extension)

A tiny Manifest V3 extension that reads your **live** Jira/Confluence session
cookies and **syncs** them to local tools via Chrome **Native Messaging** (or
downloads a JSON file for [`atlassian-cli import`](../AGENT_USAGE.md)).

## Why an extension (and not Playwright / cookie-DB decryption)

Reading cookies from Chrome's on-disk SQLite DB no longer works for modern
Chrome: **Chrome 127+ "app-bound encryption" (v20)** seals the cookie key to the
Chrome app, so values can't be decrypted out of band. This extension sidesteps
that entirely — running *inside* Chrome, `chrome.cookies.getAll` returns the
cookies in **plaintext**, including **HttpOnly** session cookies, with no
decryption, no Keychain, and no browser automation.

It reads cookies with the `{ url }` filter, so it captures exactly what the
browser would send to your Jira/Confluence host — including parent-domain
cookies such as `.atlassian.net`.

## Install (load unpacked)

1. Open `chrome://extensions`.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked** and select this `chrome-extension/` folder.
4. Pin the extension and click its icon.

Expected extension id (pinned via the manifest `key` field):
`eiknaofpjmgjacfiihcmeifjmepobkla`.

The same folder loads unchanged in Edge (`edge://extensions`), Brave, and Arc.

## One-time: register the native host

From the repo root, with `JIRA_URL` and `CONFLUENCE_URL` set (same values as MCP):

```bash
export JIRA_URL="https://yourco.atlassian.net"
export CONFLUENCE_URL="https://yourco.atlassian.net"
./atlassian-cli install-host
```

Default registers **Google Chrome only**. Other browsers:

```bash
./atlassian-cli install-host --browsers brave
./atlassian-cli install-host --all-browsers
```

This writes:

- `.atlassian-native-host-env.json` — URLs Chrome-launched host processes need
  (Chrome does not pass your shell environment)
- Native Messaging manifest under Chrome’s `NativeMessagingHosts/` dir
  pointing at `./atlassian-native-host`

Then **reload** the unpacked extension on `chrome://extensions`.

## Use

1. Sign into Jira/Confluence in this browser if needed.
2. Click the extension icon; enter your **Jira** and **Confluence** hosts
   (Cloud: usually the same tenant host).
3. Click **Sync cookies** — the extension sends cookies to the native host,
   which writes the per-service jars and probes the REST API.
4. Optional: **Download JSON only** if the host is not installed; then:

   ```bash
   atlassian-cli import ~/Downloads/atlassian-cookies.json
   ```

Re-sync whenever the session expires.

## Permissions

- `cookies`, `storage`, `nativeMessaging` (declared).
- Host access is **optional** and requested at runtime only for the specific
  origins you enter — nothing is granted until you click Sync/Download and approve.

## Security

Cookies are session credentials. Prefer **Sync** (in-process handoff to the
local host; jars written mode `0600`) over leaving JSON in Downloads.
`atlassian-cli import` still deletes a download after writing jars. The
`.atlassian-*` jars, host env file, and any `*cookies*.json` files are
git-ignored; never commit them.

The native host only accepts messages from this extension’s id (see
`allowed_origins` in the host manifest).

## Managed / corporate Chrome caveat

If your Chrome is managed by corporate policy, **Developer mode**, unpacked
extensions, or **native messaging** may be blocked. If Sync fails with a host
error and `install-host` cannot register, use **Download JSON only** +
`atlassian-cli import`, or ask IT to allowlist the extension id and host name
`com.atlassian_browser_mcp.cookie_host`.
