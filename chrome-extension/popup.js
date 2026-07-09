// Atlassian Cookie Exporter — popup logic.
//
// Reads live cookies for the configured Jira/Confluence host(s) via
// chrome.cookies.getAll({ url }) — which returns exactly the cookies the browser
// would send to that URL (including parent-domain cookies like `.atlassian.net`,
// which a { domain } query would miss) and, unlike document.cookie, INCLUDES
// HttpOnly cookies in plaintext. No decryption, no Keychain, and none of the
// Chrome 127+ app-bound-encryption problem that blocks reading the cookie DB
// off disk.
//
// Primary path: Chrome Native Messaging → local host → per-service jars
// (atlassian-cli install-host). Fallback: download atlassian-cookies.json for
// `atlassian-cli import`.

const $ = (id) => document.getElementById(id);

// Must match atlassian_native_host.NATIVE_HOST_NAME / install-host registration.
const NATIVE_HOST = "com.atlassian_browser_mcp.cookie_host";

function setStatus(msg, cls) {
  const el = $("status");
  el.textContent = msg;
  el.className = cls || "";
}

// Accept a bare host ("yourco.atlassian.net") or a full URL; return a canonical
// "https://host/" origin string, or null if it can't be parsed as http(s).
function normalizeToUrl(input) {
  const s = (input || "").trim();
  if (!s) return null;
  let u;
  try {
    u = new URL(s.includes("://") ? s : "https://" + s);
  } catch {
    return null;
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return null;
  return u.origin + "/";
}

// chrome.cookies.Cookie -> Playwright storage_state cookie shape. Session
// cookies (no expirationDate) become expires:-1, matching the auth core's
// `expires in (None, -1, 0)` session handling.
function mapCookie(c) {
  const out = {
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path || "/",
    secure: !!c.secure,
    httpOnly: !!c.httpOnly,
    expires: c.session || !c.expirationDate ? -1 : Math.floor(c.expirationDate),
  };
  const ss = { no_restriction: "None", lax: "Lax", strict: "Strict" }[c.sameSite];
  if (ss) out.sameSite = ss;
  return out;
}

const dedupeKey = (c) => `${c.name}\t${c.domain}\t${c.path}`;

async function loadSaved() {
  const { jiraHost = "", confluenceHost = "" } = await chrome.storage.local.get([
    "jiraHost",
    "confluenceHost",
  ]);
  $("jira").value = jiraHost;
  $("confluence").value = confluenceHost;
}

async function collectCookies() {
  const jira = normalizeToUrl($("jira").value);
  const conf = normalizeToUrl($("confluence").value);
  const urls = [];
  if (jira) urls.push(jira);
  if (conf && conf !== jira) urls.push(conf); // one origin on Cloud (shared host)
  if (!urls.length) {
    throw new Error("Enter at least one valid Jira or Confluence host.");
  }

  // Persist raw input so the fields are prefilled next time.
  await chrome.storage.local.set({
    jiraHost: $("jira").value.trim(),
    confluenceHost: $("confluence").value.trim(),
  });

  // Request host permission only for the origins the user configured
  // (least privilege; granted once, then remembered by Chrome).
  const origins = urls.map((u) => u + "*");
  let granted;
  try {
    granted = await chrome.permissions.request({ origins });
  } catch (e) {
    throw new Error("Permission request failed: " + e.message);
  }
  if (!granted) {
    throw new Error("Host permission denied — cannot read cookies for those hosts.");
  }

  const byKey = new Map();
  for (const url of urls) {
    let cookies;
    try {
      cookies = await chrome.cookies.getAll({ url });
    } catch (e) {
      throw new Error("cookies.getAll failed for " + url + ": " + e.message);
    }
    for (const c of cookies) byKey.set(dedupeKey(c), mapCookie(c));
  }

  const cookies = [...byKey.values()];
  if (!cookies.length) {
    throw new Error(
      "No cookies found. Sign in to Jira/Confluence in this browser, then retry.",
    );
  }
  return { cookies, urls };
}

function downloadJson(cookies) {
  const blob = new Blob([JSON.stringify({ cookies, origins: [] }, null, 2)], {
    type: "application/json",
  });
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = "atlassian-cookies.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 5000);
}

function sendNative(payload) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        resolve(response);
      });
    } catch (e) {
      reject(e);
    }
  });
}

function formatServiceLines(services) {
  if (!services || typeof services !== "object") return "";
  const lines = [];
  for (const [name, info] of Object.entries(services)) {
    if (!info) continue;
    if (info.skipped) {
      lines.push(`${name}: skipped (${info.message || "no match"})`);
      continue;
    }
    const st = info.status == null ? "?" : info.status;
    const live = st === 200 ? "live" : "NOT live";
    lines.push(`${name}: ${info.matched || 0} cookies → HTTP ${st} (${live})`);
  }
  return lines.join("\n");
}

async function syncCookies() {
  setStatus("Working…");
  try {
    const { cookies, urls } = await collectCookies();
    let response;
    try {
      response = await sendNative({ cmd: "import", cookies });
    } catch (e) {
      setStatus(
        "Native host not available (" +
          e.message +
          ").\nRun: atlassian-cli install-host\n" +
          "Or use “Download JSON only”, then:\n" +
          "atlassian-cli import ~/Downloads/atlassian-cookies.json",
        "err",
      );
      return;
    }

    if (!response || typeof response !== "object") {
      setStatus("Native host returned an empty response.", "err");
      return;
    }
    if (response.error && !response.any_matched) {
      setStatus("Sync failed: " + response.error, "err");
      return;
    }

    const lines = formatServiceLines(response.services);
    if (response.ok || response.any_live) {
      setStatus(
        `Synced ${cookies.length} cookies from ${urls.length} host(s).\n${lines}`,
        "ok",
      );
    } else {
      setStatus(
        `Imported cookies but session is NOT live.\n${lines}\n` +
          (response.error ? response.error + "\n" : "") +
          "Sign into Jira/Confluence in this browser and Sync again.",
        "err",
      );
    }
  } catch (e) {
    setStatus(e.message || String(e), "err");
  }
}

async function downloadOnly() {
  setStatus("Working…");
  try {
    const { cookies, urls } = await collectCookies();
    downloadJson(cookies);
    setStatus(
      `Exported ${cookies.length} cookies from ${urls.length} host(s) → atlassian-cookies.json\n` +
        "Then: atlassian-cli import ~/Downloads/atlassian-cookies.json",
      "ok",
    );
  } catch (e) {
    setStatus(e.message || String(e), "err");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadSaved();
  $("sync").addEventListener("click", syncCookies);
  $("download").addEventListener("click", downloadOnly);
});
