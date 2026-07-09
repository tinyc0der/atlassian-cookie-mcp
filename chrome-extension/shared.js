// Shared helpers for popup + background service worker (ES modules).

export const NATIVE_HOST = "com.atlassian_browser_mcp.cookie_host";

/** Storage key: user opted into cookies.onChanged auto-sync. Default off. */
export const AUTO_SYNC_KEY = "autoSync";

/** Debounce window after a meaningful session-cookie change (ms). */
export const AUTO_SYNC_DEBOUNCE_MS = 3000;

/**
 * Cookie names that warrant a jar refresh (login / session rotation).
 * Ignore churn like XSRF tokens and load-balancer cookies.
 */
const SESSION_COOKIE_NAMES = new Set([
  "tenant.session.token",
  "cloud.session.token",
  "jsessionid",
  "seraph.rememberme.cookie",
  "studio.crowd.tokenkey",
  "crowd.token_key",
  "atlassian.account.session",
  "atl.account.session",
]);

export function isSessionCookieName(name) {
  if (!name) return false;
  const n = String(name).toLowerCase();
  if (SESSION_COOKIE_NAMES.has(n)) return true;
  // Broad but still session-ish (Server/DC variants).
  if (n.includes("session") && (n.includes("token") || n.includes("jsession"))) {
    return true;
  }
  return false;
}

export function normalizeHost(host) {
  return (host || "").toLowerCase().replace(/^\./, "");
}

export function hostnamesMatch(a, b) {
  a = normalizeHost(a);
  b = normalizeHost(b);
  if (!a || !b) return false;
  return a === b || a.endsWith("." + b) || b.endsWith("." + a);
}

export function isKnownAtlassianCloudHost(host) {
  const h = normalizeHost(host);
  return (
    h === "atlassian.net" ||
    h.endsWith(".atlassian.net") ||
    h === "jira.com" ||
    h.endsWith(".jira.com")
  );
}

export function isAllowedProductHost(host, allowedHosts) {
  const h = normalizeHost(host);
  if (!h) return false;
  if (Array.isArray(allowedHosts) && allowedHosts.length) {
    for (const a of allowedHosts) {
      if (hostnamesMatch(h, a)) return true;
    }
  }
  return isKnownAtlassianCloudHost(h);
}

export function cookieBelongsToPageHost(cookie, pageHost) {
  const d = normalizeHost(cookie.domain || "");
  const h = normalizeHost(pageHost);
  if (!d || !h) return false;
  return h === d || h.endsWith("." + d) || d.endsWith("." + h);
}

export function mapCookie(c) {
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

export const dedupeKey = (c) => `${c.name}\t${c.domain}\t${c.path}`;

export function originForHost(host) {
  const h = normalizeHost(host);
  return `https://${h}/`;
}

export function sendNative(payload) {
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

export async function fetchAllowedHosts() {
  try {
    const reply = await sendNative({ cmd: "ping" });
    if (reply && Array.isArray(reply.allowed_hosts)) {
      return reply.allowed_hosts.map(normalizeHost).filter(Boolean);
    }
  } catch {
    // Host not installed — Cloud heuristics only.
  }
  return [];
}

export async function getAutoSyncEnabled() {
  const { [AUTO_SYNC_KEY]: v } = await chrome.storage.local.get({
    [AUTO_SYNC_KEY]: false,
  });
  return !!v;
}

export async function setAutoSyncEnabled(enabled) {
  await chrome.storage.local.set({ [AUTO_SYNC_KEY]: !!enabled });
}

/**
 * Collect cookies for a product host (url filter + domain filter).
 * Requires host permission (declared or optional) for that origin.
 */
export async function collectCookiesForHost(pageHost) {
  const host = normalizeHost(pageHost);
  const origin = originForHost(host);
  let cookies;
  try {
    cookies = await chrome.cookies.getAll({ url: origin });
  } catch (e) {
    throw new Error("cookies.getAll failed: " + e.message);
  }
  const byKey = new Map();
  for (const c of cookies) {
    if (!cookieBelongsToPageHost(c, host)) continue;
    byKey.set(dedupeKey(c), mapCookie(c));
  }
  const list = [...byKey.values()];
  if (!list.length) {
    throw new Error(
      "No cookies for " + host + ". Sign into Jira/Confluence, then retry.",
    );
  }
  return { cookies: list, origin, host };
}

/** Push cookies for host to native host. Returns host reply. */
export async function importCookiesForHost(pageHost) {
  const { cookies, origin, host } = await collectCookiesForHost(pageHost);
  const response = await sendNative({
    cmd: "import",
    cookies,
    page_host: host,
    page_origin: origin,
  });
  return { response, cookies, host, origin };
}

/**
 * Optional host permissions for custom (non-Cloud) install-host URLs.
 * Cloud hosts are covered by declared host_permissions.
 */
export function optionalOriginsForHosts(hosts) {
  const origins = [];
  for (const h of hosts || []) {
    const host = normalizeHost(h);
    if (!host || isKnownAtlassianCloudHost(host)) continue;
    origins.push(`https://${host}/*`);
    origins.push(`http://${host}/*`);
  }
  return origins;
}

export async function ensureHostPermissions(hosts) {
  const origins = optionalOriginsForHosts(hosts);
  if (!origins.length) return true;
  try {
    const have = await chrome.permissions.contains({ origins });
    if (have) return true;
    return await chrome.permissions.request({ origins });
  } catch {
    return false;
  }
}

export function formatServiceLines(services) {
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
