// Atlassian Cookie Sync — popup logic.
//
// Reads live cookies for the *current tab's origin* via chrome.cookies.getAll
// (includes HttpOnly; sidesteps Chrome 127+ app-bound cookie encryption) and
// hands them to the local native host (atlassian-cli install-host) which writes
// per-service jars. No host fields, no Downloads.

const $ = (id) => document.getElementById(id);

// Must match atlassian_native_host.NATIVE_HOST_NAME / install-host registration.
const NATIVE_HOST = "com.atlassian_browser_mcp.cookie_host";

function setStatus(msg, cls) {
  const el = $("status");
  el.textContent = msg;
  el.className = cls || "";
}

function setHostLabel(text, isError) {
  const el = $("host");
  el.textContent = "";
  if (isError) {
    el.textContent = text;
    el.style.color = "#d93025";
    return;
  }
  el.style.color = "";
  el.appendChild(document.createTextNode("Tab: "));
  const strong = document.createElement("strong");
  strong.textContent = text;
  el.appendChild(strong);
}

// chrome.cookies.Cookie -> storage_state cookie shape. Session cookies
// (no expirationDate) become expires:-1.
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

/** @returns {Promise<{ origin: string, host: string }>} */
async function getActiveOrigin() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || !tab.url) {
    throw new Error("No active tab URL. Open a Jira/Confluence page, then try again.");
  }
  let u;
  try {
    u = new URL(tab.url);
  } catch {
    throw new Error("Could not parse the current tab URL.");
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") {
    throw new Error(
      "Current tab is not http(s). Switch to your Jira/Confluence site, then Sync.",
    );
  }
  return { origin: u.origin + "/", host: u.hostname };
}

async function collectCookiesForOrigin(origin) {
  // activeTab grants cookie access for the current tab's origin while the
  // popup is open after the user clicked the action icon.
  let cookies;
  try {
    cookies = await chrome.cookies.getAll({ url: origin });
  } catch (e) {
    throw new Error("cookies.getAll failed: " + e.message);
  }
  const byKey = new Map();
  for (const c of cookies) byKey.set(dedupeKey(c), mapCookie(c));
  const list = [...byKey.values()];
  if (!list.length) {
    throw new Error(
      "No cookies for this tab. Sign into Jira/Confluence here, then Sync again.",
    );
  }
  return list;
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
  const btn = $("sync");
  btn.disabled = true;
  setStatus("Working…");
  try {
    const { origin, host } = await getActiveOrigin();
    setHostLabel(host);
    const cookies = await collectCookiesForOrigin(origin);

    let response;
    try {
      response = await sendNative({ cmd: "import", cookies });
    } catch (e) {
      setStatus(
        "Native host not available (" +
          e.message +
          ").\nRun once:\n  atlassian-cli install-host\nThen reload this extension and Sync again.",
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
      setStatus(`Synced ${cookies.length} cookies from ${host}.\n${lines}`, "ok");
    } else {
      setStatus(
        `Imported cookies but session is NOT live.\n${lines}\n` +
          (response.error ? response.error + "\n" : "") +
          "Sign in on this tab and Sync again.",
        "err",
      );
    }
  } catch (e) {
    setStatus(e.message || String(e), "err");
  } finally {
    btn.disabled = false;
  }
}

async function showActiveHost() {
  try {
    const { host } = await getActiveOrigin();
    setHostLabel(host);
    $("sync").disabled = false;
  } catch (e) {
    setHostLabel(e.message || String(e), true);
    $("sync").disabled = true;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("sync").addEventListener("click", syncCookies);
  showActiveHost();
});
