// Service worker for the Codex Switcher side panel.
// Two jobs only:
//   1. keep the toolbar badge showing the remaining 5h quota of the account in
//      ~/.codex/auth.json (one alarm-driven poll per minute, cached server side);
//   2. notify when that account runs out, so switching is one click away.
// All request logic for the panel lives in sidepanel.js — extension pages have
// host permissions, so they fetch 127.0.0.1 directly and stay usable while this
// worker is asleep.

const API = "http://127.0.0.1:8787";
const ALARM = "cs-quota-poll";
const SNAPSHOT_KEY = "cs_snapshot";
const EXHAUSTED_KEY = "cs_prev_exhausted";

const COLORS = {
  ok: "#10b981",
  warn: "#f59e0b",
  bad: "#ef4444",
  off: "#71717a",
};

async function apiGet(path, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(API + path, {
      signal: controller.signal,
      cache: "no-store",
    });
    const text = await response.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      data = { raw: text };
    }
    if (!response.ok) {
      const error = new Error(data.message || "HTTP " + response.status);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function remainingPercent(quota, key) {
  const window = quota && quota[key] ? quota[key] : null;
  if (!window || typeof window.used_percent !== "number") return null;
  return Math.max(0, Math.min(100, 100 - window.used_percent));
}

async function setBadge(text, color, title) {
  await chrome.action.setBadgeText({ text });
  if (color) {
    await chrome.action.setBadgeBackgroundColor({ color });
  }
  await chrome.action.setTitle({ title });
}

async function paint(snapshot) {
  if (!snapshot.online) {
    await setBadge("!", COLORS.off, "Codex Switcher：本地服务未启动（127.0.0.1:8787）");
    return;
  }
  if (!snapshot.logged_in) {
    await setBadge("–", COLORS.off, "Codex Switcher：没有登录中的 auth.json");
    return;
  }
  if (!snapshot.quota_ok) {
    await setBadge("?", COLORS.off, "Codex Switcher：额度读取失败（" + (snapshot.quota_error || "unknown") + "）");
    return;
  }
  const five = snapshot.remaining_5h;
  const week = snapshot.remaining_7d;
  const text = five === null ? "?" : `${five}%`;
  let color = COLORS.ok;
  if (snapshot.limit_reached || (five !== null && five <= 0)) color = COLORS.bad;
  else if (five !== null && five < 20) color = COLORS.bad;
  else if (five !== null && five < 50) color = COLORS.warn;
  const title =
    `${snapshot.email || "未登录"}` +
    (snapshot.plan ? ` · ${snapshot.plan}` : "") +
    (five === null ? "" : `\n5h 剩余 ${five}%`) +
    (week === null ? "" : ` · 7d 剩余 ${week}%`) +
    (snapshot.limit_reached ? "\n已用尽，点图标切换账号" : "");
  await setBadge(text, color, title);
}

async function maybeNotify(snapshot) {
  const stored = await chrome.storage.local.get({ [EXHAUSTED_KEY]: false });
  const was = !!stored[EXHAUSTED_KEY];
  const now = !!(snapshot.online && snapshot.logged_in && snapshot.quota_ok && snapshot.limit_reached);
  if (now && !was) {
    const settings = await chrome.storage.local.get({ notify_on_exhausted: true });
    if (settings.notify_on_exhausted) {
      chrome.notifications.create("cs-exhausted-" + Date.now(), {
        type: "basic",
        iconUrl: "icons/icon128.png",
        title: "Codex 额度已用尽",
        message: (snapshot.email || "当前账号") + " 的 5h 额度用完了，点侧边栏切换账号。",
        priority: 2,
      });
    }
  }
  await chrome.storage.local.set({ [EXHAUSTED_KEY]: now });
}

async function refresh() {
  let snapshot;
  try {
    const status = await apiGet("/api/status");
    const auth = status.auth || {};
    snapshot = {
      ok: true,
      online: true,
      at: Date.now(),
      logged_in: !!auth.logged_in,
      email: auth.email || "",
      name: auth.name || "",
      plan: auth.plan || "",
      accounts_total: (status.accounts && status.accounts.total) || 0,
      switching_enabled: !!(status.switching && status.switching.enabled),
      switching_message: (status.switching && status.switching.message) || "",
    };
    if (!snapshot.logged_in) {
      snapshot.quota_ok = false;
      snapshot.quota_error = "not_logged_in";
      snapshot.remaining_5h = null;
      snapshot.remaining_7d = null;
      snapshot.limit_reached = false;
    } else {
      const payload = await apiGet("/api/quota");
      const quota = payload.quota || {};
      snapshot.quota_ok = !!quota.ok;
      snapshot.quota_error = quota.error || null;
      snapshot.plan = quota.plan || snapshot.plan;
      snapshot.limit_reached = !!quota.limit_reached;
      snapshot.remaining_5h = quota.ok ? remainingPercent(quota, "primary_window") : null;
      snapshot.remaining_7d = quota.ok ? remainingPercent(quota, "secondary_window") : null;
      snapshot.used_5h = quota.primary_window ? quota.primary_window.used_percent : null;
      snapshot.used_7d = quota.secondary_window ? quota.secondary_window.used_percent : null;
      snapshot.reset_at_5h = quota.primary_window ? quota.primary_window.reset_at : null;
      snapshot.reset_at_7d = quota.secondary_window ? quota.secondary_window.reset_at : null;
    }
  } catch (error) {
    snapshot = {
      ok: false,
      online: false,
      at: Date.now(),
      error: error && error.message ? error.message : "offline",
      logged_in: false,
      quota_ok: false,
      remaining_5h: null,
      remaining_7d: null,
      limit_reached: false,
    };
  }
  await chrome.storage.local.set({ [SNAPSHOT_KEY]: snapshot });
  await paint(snapshot);
  await maybeNotify(snapshot);
  return snapshot;
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(ALARM);
  if (!existing) {
    chrome.alarms.create(ALARM, { periodInMinutes: 1, delayInMinutes: 0.1 });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await ensureAlarm();
  try {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch (_) {
    /* older Chrome: the side panel still opens from the toolbar menu */
  }
  await chrome.storage.local.set({ notify_on_exhausted: true });
  await refresh();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  await refresh();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) refresh();
});

chrome.notifications.onClicked.addListener((notificationId) => {
  if (!notificationId.startsWith("cs-exhausted-")) return;
  // No gesture is available for sidePanel.open() from a notification, so send
  // the user to the full dashboard, where switching is one click away.
  chrome.tabs.create({ url: API + "/" });
  chrome.notifications.clear(notificationId);
});

// The side panel and the dashboard both mutate accounts; let them ask for an
// immediate badge refresh instead of waiting for the next alarm.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "cs-refresh-badge") {
    refresh().then((snapshot) => sendResponse({ ok: true, snapshot }));
    return true;
  }
  if (message && message.type === "cs-set-notify") {
    chrome.storage.local
      .set({ notify_on_exhausted: !!message.enabled })
      .then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});
