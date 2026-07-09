// Background service worker: opt-in auto-sync on session cookie changes.
//
// When autoSync is enabled (popup toggle), listens to chrome.cookies.onChanged
// for *session/identity* cookies only (tenant.session.token, JSESSIONID, …) —
// that is when the jar must be refreshed. On trigger it still pushes the *full*
// product-domain cookie set (browser-like), not just the session cookie.
// Default is off. Manual Sync in the popup is unchanged.

import {
  AUTO_SYNC_DEBOUNCE_MS,
  fetchAllowedHosts,
  getAutoSyncEnabled,
  importCookiesForHost,
  isAllowedProductHost,
  isSessionCookieName,
  normalizeHost,
  sendNative,
} from "./shared.js";

/** @type {Map<string, ReturnType<typeof setTimeout>>} */
const pendingByHost = new Map();

/** Last auto-sync result for the popup to display. */
async function setLastAutoSync(payload) {
  await chrome.storage.local.set({
    lastAutoSync: {
      ...payload,
      at: Date.now(),
    },
  });
}

async function resolveAllowedHosts() {
  return fetchAllowedHosts();
}

/**
 * Pick a stable product hostname from a cookie domain
 * (e.g. ".atlassian.net" alone is not enough — need tenant host from change
 * or from allowed_hosts / cookie domain if it is a full host).
 */
function pageHostFromCookieDomain(domain, allowedHosts) {
  const d = normalizeHost(domain);
  if (!d) return null;
  // Prefer an allowed host that matches this domain.
  for (const a of allowedHosts || []) {
    if (d === a || a.endsWith("." + d) || d.endsWith("." + a) || d === normalizeHost(a)) {
      // If cookie is on parent .atlassian.net, use configured tenant.
      if (isAllowedProductHost(a, allowedHosts)) return normalizeHost(a);
    }
  }
  // Cookie set on the tenant host itself.
  if (isAllowedProductHost(d, allowedHosts)) return d;
  // Parent-domain cookie (e.g. .atlassian.net): map to first allowed Cloud host.
  for (const a of allowedHosts || []) {
    if (a.endsWith(".atlassian.net") || a.endsWith(".jira.com")) return a;
  }
  return null;
}

async function runAutoSync(pageHost, reason) {
  const host = normalizeHost(pageHost);
  if (!host) return;
  try {
    const { response, cookies } = await importCookiesForHost(host);
    const ok = !!(response && (response.ok || response.any_live));
    await setLastAutoSync({
      ok,
      host,
      reason,
      cookieCount: cookies.length,
      error: response && response.error ? response.error : null,
      services: response && response.services ? response.services : null,
    });
    console.log(
      "[atlassian-cookie-sync] auto-sync",
      host,
      ok ? "ok" : "not-live",
      reason,
    );
  } catch (e) {
    await setLastAutoSync({
      ok: false,
      host,
      reason,
      error: e.message || String(e),
    });
    console.warn("[atlassian-cookie-sync] auto-sync failed", host, e);
  }
}

function scheduleAutoSync(pageHost, reason) {
  const host = normalizeHost(pageHost);
  if (!host) return;
  const prev = pendingByHost.get(host);
  if (prev) clearTimeout(prev);
  const t = setTimeout(() => {
    pendingByHost.delete(host);
    runAutoSync(host, reason);
  }, AUTO_SYNC_DEBOUNCE_MS);
  pendingByHost.set(host, t);
}

chrome.cookies.onChanged.addListener(async (changeInfo) => {
  try {
    if (!(await getAutoSyncEnabled())) return;

    const cookie = changeInfo.cookie;
    if (!cookie || !isSessionCookieName(cookie.name)) return;

    const allowedHosts = await resolveAllowedHosts();
    const pageHost = pageHostFromCookieDomain(cookie.domain, allowedHosts);
    if (!pageHost) return;
    if (!isAllowedProductHost(pageHost, allowedHosts)) return;

    const reason = changeInfo.removed
      ? `removed:${cookie.name}`
      : `changed:${cookie.name}`;
    scheduleAutoSync(pageHost, reason);
  } catch (e) {
    console.warn("[atlassian-cookie-sync] onChanged handler error", e);
  }
});

// Popup / other extension pages can ask the worker to sync a host immediately.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "autoSyncNow") return false;
  (async () => {
    try {
      const host = normalizeHost(msg.host);
      if (!host) {
        sendResponse({ ok: false, error: "missing host" });
        return;
      }
      const allowedHosts = await resolveAllowedHosts();
      if (!isAllowedProductHost(host, allowedHosts)) {
        sendResponse({ ok: false, error: "host not allowed: " + host });
        return;
      }
      await runAutoSync(host, msg.reason || "manual-trigger");
      const { lastAutoSync } = await chrome.storage.local.get("lastAutoSync");
      sendResponse({ ok: true, lastAutoSync });
    } catch (e) {
      sendResponse({ ok: false, error: e.message || String(e) });
    }
  })();
  return true; // async sendResponse
});

// Warm native host / permissions when auto-sync is turned on.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.autoSync) return;
  if (changes.autoSync.newValue) {
    // Touch allowed hosts so install-host env is exercised early.
    fetchAllowedHosts().catch(() => {});
    // Optional: verify native host is reachable.
    sendNative({ cmd: "ping" }).catch((e) => {
      console.warn(
        "[atlassian-cookie-sync] native host unreachable after enabling auto-sync:",
        e.message,
      );
    });
  }
});

console.log("[atlassian-cookie-sync] service worker loaded");
