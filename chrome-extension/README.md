# Atlassian Cookie Exporter (Chrome extension)

A tiny Manifest V3 extension that exports your **live** Jira/Confluence session
cookies to a JSON file, which [`atlassian-cli import`](../AGENT_USAGE.md) loads
into the cookie jar the MCP server and CLI reuse.

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

The same folder loads unchanged in Edge (`edge://extensions`), Brave, and Arc.

## Use

1. Click the extension icon.
2. Enter your **Jira host** and **Confluence host** (e.g.
   `https://yourco.atlassian.net`). On Atlassian Cloud these are usually the same
   host — enter it in both (or just one); duplicates are de-duped.
3. Click **Export cookies**. On first use Chrome asks permission to read cookies
   for those hosts — allow it. A file `atlassian-cookies.json` downloads.
4. Import it:

   ```bash
   atlassian-cli import ~/Downloads/atlassian-cookies.json
   ```

   The CLI splits the cookies into the Jira and Confluence jars and verifies each
   is live (HTTP 200). Re-export whenever the session expires.

## Permissions

- `cookies` + `storage` (declared).
- Host access is **optional** and requested at runtime only for the specific
  origins you enter — nothing is granted until you click Export and approve.

## Security

The exported JSON contains **live session cookies** — treat it like a password.
Delete the download after importing. The `.atlassian-*` jars and any
`*cookies*.json` files are git-ignored; never commit them.

## Managed / corporate Chrome caveat

If your Chrome is managed by corporate policy, **Developer mode** or unpacked
extensions may be blocked (`ExtensionInstallBlocklist`, developer-mode disabled).
If "Load unpacked" is greyed out or the extension won't run, you'll need IT to
allowlist the extension ID, or pack and self-host it — outside the scope of this
repo.
