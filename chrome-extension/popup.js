// Atlassian Cookie Sync — popup UI (manual Sync + auto-sync toggle).

import {
  ensureHostPermissions,
  fetchAllowedHosts,
  formatServiceLines,
  getAutoSyncEnabled,
  importCookiesForHost,
  isAllowedProductHost,
  setAutoSyncEnabled,
} from "./shared.js";

const $ = (id) => document.getElementById(id);

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

async function syncCookies() {
  const btn = $("sync");
  btn.disabled = true;
  setStatus("Working…");
  try {
    const { host } = await requireProductTab();
    setHostLabel(host);
    const { response, cookies } = await importCookiesForHost(host);

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
    const msg = e.message || String(e);
    if (/native|host|Specified native/i.test(msg)) {
      setStatus(
        "Native host not available (" +
          msg +
          ").\nRun once:\n  atlassian-cli install-host\nThen reload this extension and Sync again.",
        "err",
      );
    } else {
      setStatus(msg, "err");
    }
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

async function showLastAutoSync() {
  const { lastAutoSync } = await chrome.storage.local.get("lastAutoSync");
  const el = $("lastAuto");
  if (!el) return;
  if (!lastAutoSync || !lastAutoSync.at) {
    el.textContent = "";
    return;
  }
  const ago = Math.round((Date.now() - lastAutoSync.at) / 1000);
  const when =
    ago < 60 ? `${ago}s ago` : ago < 3600 ? `${Math.round(ago / 60)}m ago` : "earlier";
  if (lastAutoSync.ok) {
    el.textContent = `Last auto-sync: ${lastAutoSync.host} OK (${when})`;
    el.className = "last ok";
  } else {
    el.textContent = `Last auto-sync failed: ${lastAutoSync.error || "unknown"} (${when})`;
    el.className = "last err";
  }
}

async function loadAutoSyncToggle() {
  const enabled = await getAutoSyncEnabled();
  $("autoSync").checked = enabled;
}

async function onAutoSyncToggle() {
  const want = $("autoSync").checked;
  if (want) {
    // Request optional permissions for custom DC hosts from install-host.
    const allowed = await fetchAllowedHosts();
    const granted = await ensureHostPermissions(allowed);
    if (!granted && allowed.some((h) => !h.endsWith(".atlassian.net") && !h.endsWith(".jira.com"))) {
      $("autoSync").checked = false;
      setStatus(
        "Host permission denied for custom Jira/Confluence host. Allow it to enable auto-sync.",
        "err",
      );
      return;
    }
    // Smoke-check native host.
    try {
      await fetchAllowedHosts();
    } catch {
      /* ignore */
    }
  }
  await setAutoSyncEnabled(want);
  setStatus(
    want
      ? "Auto-sync on. Session cookie changes will update local jars (Chrome must stay open)."
      : "Auto-sync off. Use Sync cookies manually.",
    want ? "ok" : "",
  );
}

document.addEventListener("DOMContentLoaded", () => {
  $("sync").addEventListener("click", syncCookies);
  $("autoSync").addEventListener("change", onAutoSyncToggle);
  loadAutoSyncToggle();
  showActiveHost();
  showLastAutoSync();
});
