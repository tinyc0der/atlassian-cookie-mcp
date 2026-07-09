# Atlassian Cookie Sync (Chrome extension)

A tiny Manifest V3 extension that reads your **live** Jira/Confluence session
cookies from the **current tab** and **syncs** them to local tools via Chrome
**Native Messaging** ([`atlassian-cli install-host`](../AGENT_USAGE.md)).

## Why an extension (and not Playwright / cookie-DB decryption)

Reading cookies from Chrome's on-disk SQLite DB no longer works for modern
Chrome: **Chrome 127+ "app-bound encryption" (v20)** seals the cookie key to the
Chrome app, so values can't be decrypted out of band. This extension sidesteps
that entirely — running *inside* Chrome, `chrome.cookies.getAll` returns the
cookies in **plaintext**, including **HttpOnly** session cookies, with no
decryption, no Keychain, and no browser automation.

It uses the active tab’s origin, so it captures exactly what the browser would
send to that host — including parent-domain cookies such as `.atlassian.net`.

## Install (load unpacked)

1. Open `chrome://extensions`.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked** and select this `chrome-extension/` folder.
4. Pin the extension and click its icon.

Expected extension id (pinned via the manifest `key` field):
`eiknaofpjmgjacfiihcmeifjmepobkla`.

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

1. Sign into Jira or Confluence in Chrome.
2. Stay on a page on that host (e.g. your site’s issue list or a Confluence page).
3. Click the extension → **Sync cookies**.

The extension **only** reads cookies when the tab is a Jira/Confluence site:

- Hosts from `JIRA_URL` / `CONFLUENCE_URL` (via `install-host`), or
- Known Atlassian Cloud hosts (`*.atlassian.net`, `*.jira.com`)

Other tabs (Google, email, etc.) are rejected before any cookie is read. Cookie
domains are also filtered to the page host. The native host re-checks `page_host`
before writing jars.

On Atlassian Cloud, Jira and Confluence share one host — one Sync covers both
(if `JIRA_URL` / `CONFLUENCE_URL` point at that tenant). On Server/DC with
separate hosts, open each product and Sync once.

Re-sync whenever the session expires.

## Permissions

- `cookies`, `activeTab`, `nativeMessaging`.
- No host list to configure: cookie access is scoped to the tab you invoke the
  extension on (`activeTab`).

## Security

Cookies are session credentials. Sync hands them to the local host over native
messaging; jars are written mode `0600`. The `.atlassian-*` jars and host env
file are git-ignored; never commit them.

The native host only accepts messages from this extension’s id (see
`allowed_origins` in the host manifest).

## Managed / corporate Chrome caveat

If your Chrome is managed by corporate policy, **Developer mode**, unpacked
extensions, or **native messaging** may be blocked. Ask IT to allowlist the
extension id and host name `com.atlassian_browser_mcp.cookie_host`.
