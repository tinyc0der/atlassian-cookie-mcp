// Atlassian Cookie Sync — popup logic.
//
// Reads live cookies only when the active tab is a Jira/Confluence host
// (configured via install-host, or known Atlassian Cloud). Hands them to the
// native host which writes per-service jars.

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

function normalizeHost(host) {
  return (host || "").toLowerCase().replace(/^\./, "");
}

function hostnamesMatch(a, b) {
  a = normalizeHost(a);
  b = normalizeHost(b);
  if (!a || !b) return false;
  return a === b || a.endsWith("." + b) || b.endsWith("." + a);
}

/** Known Atlassian Cloud product hosts (custom DC must be in install-host list). */
function isKnownAtlassianCloudHost(host) {
  const h = normalizeHost(host);
  return (
    h === "atlassian.net" ||
    h.endsWith(".atlassian.net") ||
    h === "jira.com" ||
    h.endsWith(".jira.com")
  );
}

function isAllowedProductHost(host, allowedHosts) {
  const h = normalizeHost(host);
  if (!h) return false;
  if (Array.isArray(allowedHosts) && allowedHosts.length) {
    for (const a of allowedHosts) {
      if (hostnamesMatch(h, a)) return true;
    }
  }
  return isKnownAtlassianCloudHost(h);
}

/** Cookie domain belongs to the page host (incl. parent-domain cookies). */
function cookieBelongsToPageHost(cookie, pageHost) {
  const d = normalizeHost(cookie.domain || "");
  const h = normalizeHost(pageHost);
  if (!d || !h) return false;
  return h === d || h.endsWith("." + d) || d.endsWith("." + h);
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

/** @returns {Promise<string[]>} */
async function fetchAllowedHosts() {
  try {
    const reply = await sendNative({ cmd: "ping" });
    if (reply && Array.isArray(reply.allowed_hosts)) {
      return reply.allowed_hosts.map(normalizeHost).filter(Boolean);
    }
  } catch {
    // Host not installed yet — fall back to Cloud hostname heuristics only.
  }
  return [];
}

/** @returns {Promise<{ origin: string, host: string, url: string }>} */
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
  return { origin: u.origin + "/", host: u.hostname, url: tab.url };
}

/**
 * Ensure tab is Jira/Confluence before reading any cookies.
 * @returns {Promise<{ origin: string, host: string, allowedHosts: string[] }>}
 */
async function requireProductTab() {
  const { origin, host } = await getActiveOrigin();
  const allowedHosts = await fetchAllowedHosts();
  if (!isAllowedProductHost(host, allowedHosts)) {
    const hint = allowedHosts.length
      ? "Allowed: " + allowedHosts.join(", ")
      : "Use *.atlassian.net or run atlassian-cli install-host for custom hosts";
    throw new Error(
      "Not a Jira/Confluence tab (" + host + ").\n" +
        "Open your Jira or Confluence site, then Sync.\n" +
        hint,
    );
  }
  return { origin, host, allowedHosts };
}

async function collectCookiesForOrigin(origin, pageHost) {
  // activeTab grants cookie access for the current tab's origin while the
  // popup is open after the user clicked the action icon.
  let cookies;
  try {
    cookies = await chrome.cookies.getAll({ url: origin });
  } catch (e) {
    throw new Error("cookies.getAll failed: " + e.message);
  }
  const byKey = new Map();
  for (const c of cookies) {
    if (!cookieBelongsToPageHost(c, pageHost)) continue;
    byKey.set(dedupeKey(c), mapCookie(c));
  }
  const list = [...byKey.values()];
  if (!list.length) {
    throw new Error(
      "No cookies for this Jira/Confluence tab. Sign in here, then Sync again.",
    );
  }
  return list;
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
    // Gate *before* reading cookies so random sites never get scraped.
    const { origin, host } = await requireProductTab();
    setHostLabel(host);
    const cookies = await collectCookiesForOrigin(origin, host);

    let response;
    try {
      response = await sendNative({
        cmd: "import",
        cookies,
        page_host: host,
        page_origin: origin,
      });
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
    const { host } = await requireProductTab();
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
