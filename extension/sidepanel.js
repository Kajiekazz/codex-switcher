// Side panel UI for Codex Switcher.
//
// Same 5 endpoints as the old Tampermonkey panel (/api/status, /api/quota,
// /api/snapshots, /api/accounts/import, /api/switch) plus a badge refresh ping
// to the service worker. Extension pages carry host permissions, so this file
// talks to 127.0.0.1 directly instead of going through the worker, which keeps
// the panel working while the MV3 worker is asleep.

const API = "http://127.0.0.1:8787";
const REFRESH_MS = 15000;
// Theme follows the dashboard over SSE; the poll is only the fallback (and a
// slow safety net while the stream is healthy). 2 s polling is what made the
// panel look "one beat behind" the click.
const THEME_REFRESH_MS = 2000;
const THEME_SAFETY_MS = 30000;
const DASHBOARD_URL = API + "/";

const $ = (selector) => document.querySelector(selector);

let switchState = { switching: {}, current: null, snapshots: [] };

// The header badge follows the shipped manifest so it can never drift from
// chrome://extensions. Falls back to the static text when opened outside Chrome.
(function stampVersion() {
  const badge = document.getElementById("ver");
  if (!badge) return;
  try {
    const version = chrome.runtime.getManifest().version;
    if (version) badge.textContent = "v" + version;
  } catch (_) {
    /* not running as an extension */
  }
})();

async function api(method, path, body, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || 8000);
  let response;
  try {
    response = await fetch(API + path, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (error) {
    clearTimeout(timer);
    throw new Error(
      error && error.name === "AbortError" ? "本地服务超时" : "本地服务未启动"
    );
  }
  const text = await response.text();
  clearTimeout(timer);
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (_) {
    data = { raw: text };
  }
  if (!response.ok) {
    const err = new Error(data.message || "HTTP " + response.status);
    err.data = data;
    err.status = response.status;
    throw err;
  }
  return data;
}

const apiGet = (path, timeoutMs) => api("GET", path, undefined, timeoutMs);
const apiPost = (path, body, timeoutMs) => api("POST", path, body, timeoutMs);

// The side panel runs on chrome-extension://, a different origin from the
// dashboard, so localStorage cannot be shared. The dashboard pushes its theme
// to /api/theme and this reads it back — i.e. the panel always looks like the
// main panel. `auto` defers to the OS.
const SYSTEM_DARK = window.matchMedia("(prefers-color-scheme: dark)");
const THEME_CACHE_KEY = "codex-sidepanel-theme";

function applyThemeName(name) {
  const dark = name === "dark" || (name === "auto" && SYSTEM_DARK.matches);
  const root = document.documentElement;
  if (root.classList.contains("dark") !== dark) {
    root.classList.toggle("dark", dark);
  }
  root.dataset.theme = name || "";
  try {
    localStorage.setItem(THEME_CACHE_KEY, name || "");
  } catch (_) {
    /* private mode: the head bootstrap just starts light */
  }
}

async function syncTheme() {
  try {
    const payload = await apiGet("/api/theme", 4000);
    applyThemeState(payload);
  } catch (_) {
    /* service offline: keep the current class instead of flashing */
  }
}

function applyThemeState(payload) {
  const theme = (payload && payload.theme) || "light";
  const root = document.documentElement;
  if (root.dataset.theme === theme && root.classList.contains("dark") ===
      (theme === "dark" || (theme === "auto" && SYSTEM_DARK.matches))) {
    return;
  }
  applyThemeName(theme);
}

// Server-sent events from the local service: the moment the dashboard saves a
// theme, every open side panel repaints (< 50 ms on localhost). EventSource
// reconnects by itself; `themeStreamLive` only decides whether the fallback
// poll runs often or rarely.
let themeStreamLive = false;
let themeSource = null;

function startThemeStream() {
  if (themeSource || typeof EventSource === "undefined") return;
  try {
    themeSource = new EventSource(API + "/api/theme/events");
  } catch (_) {
    themeSource = null;
    return;
  }
  themeSource.onopen = () => {
    themeStreamLive = true;
  };
  themeSource.onmessage = (event) => {
    try {
      applyThemeState(JSON.parse(event.data));
    } catch (_) {
      /* keep the last known theme */
    }
  };
  themeSource.onerror = () => {
    themeStreamLive = false;
    // A stream that cannot connect at all is not worth retrying forever
    // (service stopped): close it and let the fast poll take over.
    if (themeSource && themeSource.readyState === EventSource.CLOSED) {
      themeSource = null;
    }
  };
}

SYSTEM_DARK.addEventListener("change", () => {
  if (document.documentElement.dataset.theme === "auto") applyThemeName("auto");
});

function setConn(ok, text) {
  $("#dot").classList.toggle("online", !!ok);
  $("#dot").classList.toggle("offline", !ok);
  $("#conn").textContent = text;
}

function barColor(remaining) {
  if (remaining === null || remaining === undefined) return "";
  if (remaining < 20) return "bad";
  if (remaining < 50) return "warn";
  return "";
}

function fmtReset(epochSeconds) {
  if (!epochSeconds) return "";
  const when = new Date(epochSeconds * 1000);
  const diffMinutes = Math.round((when.getTime() - Date.now()) / 60000);
  const clock = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (diffMinutes <= 0) return "即将重置";
  if (diffMinutes < 60) return `${diffMinutes} 分钟后重置（${clock}）`;
  if (diffMinutes < 60 * 36) return `${Math.round(diffMinutes / 60)} 小时后重置（${clock}）`;
  return `${Math.round(diffMinutes / 1440)} 天后重置`;
}

function profileRef(profile) {
  return profile.profile_id || profile.email || "";
}

function workspaceLabel(profile) {
  const workspace = profile.workspace_id || profile.account_id;
  const team = ["team", "business", "enterprise", "edu"].includes(String(profile.plan || "").toLowerCase());
  return profile.workspace_name || (team ? "Team 工作区" : "个人空间") +
    (workspace ? " · " + String(workspace).slice(0, 8) : "");
}

function isCurrentProfile(profile, current) {
  if (typeof profile.is_current === "boolean") return profile.is_current;
  if (profile.profile_id && current.profile_id) return profile.profile_id === current.profile_id;
  if ((profile.workspace_id || profile.account_id || "") !== (current.workspace_id || current.account_id || "")) return false;
  if (profile.user_id && current.user_id) return profile.user_id === current.user_id;
  return !!profile.email && String(profile.email).toLowerCase() === String(current.email || "").toLowerCase();
}

function renderStatus(status) {
  setConn(true, "本地服务在线");
  const auth = status.auth || {};
  const sw = status.switching || {};
  if (auth.logged_in) {
    const who = auth.name || "";
    $("#account").textContent = (who || auth.email || "未知账号") + (auth.plan ? " · " + auth.plan : "") +
      " · " + workspaceLabel(auth);
    const mail = $("#accountMail");
    mail.textContent = auth.name ? auth.email || "" : "";
    mail.hidden = !auth.name;
    $("#account").title = auth.account_id || "";
  } else {
    $("#account").textContent = "未登录（auth.json 不存在）";
    $("#accountMail").textContent = "";
    $("#account").title = "";
  }
  const badge = $("#switchBadge");
  badge.textContent = sw.enabled ? "切号已启用" : "切号已禁用";
  badge.className = "badge" + (sw.enabled ? " ok" : "");
  badge.title = sw.message || "";
  const total = status.accounts ? status.accounts.total : 0;
  $("#poolBadge").textContent = "账号池 " + total + " 个";
}

function renderQuota(payload) {
  const quota = payload.quota || {};
  const five = quota.primary_window || null;
  const week = quota.secondary_window || null;

  const paint = (label, win, barEl, textEl) => {
    if (!win || typeof win.used_percent !== "number") {
      barEl.style.width = "0%";
      barEl.className = "";
      textEl.textContent = "—";
      return null;
    }
    const remaining = Math.max(0, Math.min(100, 100 - win.used_percent));
    // The bar fills with what is LEFT, so a full bar always means "plenty".
    barEl.style.width = remaining + "%";
    barEl.className = barColor(remaining);
    textEl.textContent = `剩 ${remaining}% · ${fmtReset(win.reset_at)}`;
    return remaining;
  };

  const fiveLeft = paint("5h", five, $("#bar5h"), $("#quota5h"));
  const weekLeft = paint("7d", week, $("#bar7d"), $("#quota7d"));

  const hint = $("#quotaHint");
  const credits = quota.reset_credits || {};
  const creditText = credits.known === false
    ? ""
    : Number(credits.applicable_available_count || credits.available_count || 0) > 0
      ? ` · 重置券 ${Number(credits.applicable_available_count || credits.available_count)} 张`
      : "";
  if (!quota.ok) {
    hint.textContent = "额度不可用" + (quota.error ? "（" + quota.error + "）" : "") + "，徽章显示 ?";
  } else if (quota.limit_reached) {
    hint.textContent = "5h 额度已用尽，建议切到有额度的账号。" + creditText;
  } else if (fiveLeft !== null) {
    hint.textContent = `工具栏徽章显示 5h 剩余 ${fiveLeft}%` + (weekLeft === null ? "" : `，7d 剩余 ${weekLeft}%`) + creditText;
  } else {
    hint.textContent = "工具栏徽章显示 5h 剩余额度。" + creditText;
  }
}

async function refreshQuota() {
  try {
    const payload = await apiGet("/api/quota");
    renderQuota(payload);
  } catch (_) {
    $("#quota5h").textContent = "—";
    $("#quota7d").textContent = "—";
    $("#bar5h").style.width = "0%";
    $("#bar7d").style.width = "0%";
    $("#quotaHint").textContent = "额度读取失败。";
  }
}

function updateSwitchButton() {
  const enabled = !!(switchState.switching && switchState.switching.enabled);
  $("#switchBtn").disabled = !enabled || !$("#switchTarget").value || !$("#switchConfirm").checked;
}

function renderSwitch(data) {
  switchState = data || { switching: {}, current: null, snapshots: [] };
  const sw = switchState.switching || {};
  const current = switchState.current || {};
  const snapshots = (switchState.snapshots || []).filter(
    (item) => item.email && !item.identity_error && !isCurrentProfile(item, current)
  );
  const enabled = !!sw.enabled;
  const toggle = $("#switchToggle");
  toggle.disabled = !enabled;
  toggle.title = sw.message || "";
  const previous = $("#switchTarget").value;
  const select = $("#switchTarget");
  select.textContent = "";
  if (!snapshots.length) {
    select.appendChild(new Option(enabled ? "没有可切换的快照" : "切号未启用", ""));
  } else {
    snapshots.forEach((item) => {
      const label =
        (item.name ? item.name + " · " : "") +
        (item.email_masked || item.email) +
        (item.plan ? " · " + item.plan : "") +
        " · " + workspaceLabel(item) +
        (item.expired ? " · token 过期" : "");
      select.appendChild(new Option(label, profileRef(item)));
    });
    if (snapshots.some((item) => profileRef(item) === previous)) select.value = previous;
  }
  const badge = $("#switchBadge");
  badge.textContent = enabled ? "切号已启用" : "切号已禁用";
  badge.className = "badge" + (enabled ? " ok" : "");
  badge.title = sw.message || "";
  updateSwitchButton();
}

async function refreshSnapshots() {
  try {
    renderSwitch(await apiGet("/api/snapshots"));
  } catch (_) {
    /* the switch select simply keeps its previous options */
  }
}

function pingBadge() {
  try {
    chrome.runtime.sendMessage({ type: "cs-refresh-badge" }, () => {
      void chrome.runtime.lastError;
    });
  } catch (_) {
    /* worker asleep or extension reloaded; the 1-minute alarm covers it */
  }
}

async function refresh() {
  // Theme is shared through /api/theme; keep it outside the /api/status
  // try-block so a status failure cannot freeze the palette.
  syncTheme();
  try {
    const status = await apiGet("/api/status");
    renderStatus(status);
    await Promise.all([refreshQuota(), refreshSnapshots()]);
    pingBadge();
  } catch (error) {
    setConn(false, error.message || "本地服务离线");
    $("#account").textContent = "先运行 python service.py";
    $("#accountMail").textContent = "";
    $("#poolBadge").textContent = "账号池 —";
    $("#switchBadge").textContent = "切号状态未知";
    $("#switchBadge").className = "badge plain";
  }
}

async function doSwitch() {
  const email = $("#switchTarget").value;
  const profile = (switchState.snapshots || []).find(item => profileRef(item) === email);
  const label = profile ? (profile.email_masked || profile.email) + " · " + workspaceLabel(profile) : email;
  const msg = $("#switchMsg");
  if (!email) {
    msg.className = "msg bad";
    msg.textContent = "先选择目标账号";
    return;
  }
  if (!$("#switchConfirm").checked) {
    msg.className = "msg bad";
    msg.textContent = "先勾选确认";
    return;
  }
  if (
    !window.confirm(
      "确认把 ~/.codex/auth.json 切换到：\n" +
        label +
        "\n\n切换前会加密备份当前 auth；写入后会重新校验；失败会自动回滚。"
    )
  ) {
    return;
  }
  msg.className = "msg";
  msg.textContent = "切换中…";
  $("#switchBtn").disabled = true;
  try {
    const data = await apiPost("/api/switch", {
      ...(profile && profile.profile_id ? { profile_id: profile.profile_id } : { email }), confirm: true,
    }, 30000);
    const sw = data.switch || {};
    msg.className = "msg ok";
    msg.textContent =
      "已切换：" + (sw.previous_email || "—") + " → " + sw.target_email +
      (sw.target_profile_id ? " · " + workspaceLabel({ workspace_name: sw.target_workspace_name,
        account_id: sw.target_account_id, plan: sw.target_plan }) : "") +
      (sw.warning ? "\n" + sw.warning : "") +
      "\n备份：" + (sw.backup_path || "—");
    $("#switchConfirm").checked = false;
    await refresh();
  } catch (error) {
    msg.className = "msg bad";
    msg.textContent =
      "切换失败：" + (error.message || "未知错误") +
      (error.data && error.data.detail ? "\n" + error.data.detail : "");
  } finally {
    updateSwitchButton();
  }
}

async function doImport() {
  const text = $("#importText").value;
  const msg = $("#importMsg");
  if (!text.trim()) {
    msg.className = "msg bad";
    msg.textContent = "先粘贴账号。";
    return;
  }
  msg.className = "msg";
  msg.textContent = "导入中…";
  try {
    const identity_type = $("#importIdentity").value || "personal";
    const result = await apiPost("/api/accounts/import", { text, identity_type }, 60000);
    msg.className = "msg ok";
    msg.textContent =
      "导入完成：新增 " + result.inserted + "，更新 " + result.updated +
      (result.errors && result.errors.length ? "，错误 " + result.errors.length : "") +
      (result.login_queued && result.login_queued.length
        ? "\n已自动排队登录 " + result.login_queued.length + " 个"
        : "");
    await refresh();
  } catch (error) {
    msg.className = "msg bad";
    msg.textContent = "导入失败：" + (error.message || "未知错误");
  }
}

$("#refreshBtn").addEventListener("click", refresh);
$("#importToggle").addEventListener("click", () => {
  const box = $("#importBox");
  box.hidden = !box.hidden;
  if (!box.hidden) $("#importText").focus();
});
$("#importCancel").addEventListener("click", () => {
  $("#importBox").hidden = true;
  $("#importMsg").textContent = "";
});
$("#importBtn").addEventListener("click", doImport);
$("#switchToggle").addEventListener("click", () => {
  const box = $("#switchBox");
  box.hidden = !box.hidden;
  if (!box.hidden) refreshSnapshots();
});
$("#switchCancel").addEventListener("click", () => {
  $("#switchBox").hidden = true;
  $("#switchMsg").textContent = "";
});
$("#switchBtn").addEventListener("click", doSwitch);
$("#switchTarget").addEventListener("change", updateSwitchButton);
$("#switchConfirm").addEventListener("change", updateSwitchButton);
$("#dashboardBtn").addEventListener("click", () => {
  chrome.tabs.create({ url: DASHBOARD_URL });
});

async function initNotifyToggle() {
  try {
    const stored = await chrome.storage.local.get({ notify_on_exhausted: true });
    $("#notifyToggle").checked = !!stored.notify_on_exhausted;
  } catch (_) {
    $("#notifyToggle").checked = true;
  }
  $("#notifyToggle").addEventListener("change", (event) => {
    try {
      chrome.runtime.sendMessage(
        { type: "cs-set-notify", enabled: event.target.checked },
        () => void chrome.runtime.lastError
      );
    } catch (_) {}
  });
}

initNotifyToggle();
syncTheme();
startThemeStream();
refresh();
setInterval(refresh, REFRESH_MS);
// Push first, poll second: when the stream is up this timer is a slow safety
// net; when it is down (service restart) the panel falls back to 2 s polling.
setInterval(() => {
  if (!themeStreamLive) syncTheme();
}, THEME_REFRESH_MS);
setInterval(() => {
  if (themeStreamLive) syncTheme();
}, THEME_SAFETY_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    syncTheme();
    refresh();
  }
});
