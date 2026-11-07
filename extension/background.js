// Service worker for the Codex Switcher side panel.
// Two jobs only:
//   1. keep the toolbar badge showing the remaining 5h quota of the account in
//      ~/.codex/auth.json (one poll per minute, with bounded pending refreshes);
//   2. notify when that account runs out, so switching is one click away.
// All request logic for the panel lives in sidepanel.js — extension pages have
// host permissions, so they fetch 127.0.0.1 directly and stay usable while this
// worker is asleep.

const API = "http://127.0.0.1:8787";
const ALARM = "cs-quota-poll";
const SNAPSHOT_KEY = "cs_snapshot";
const EXHAUSTED_KEY = "cs_prev_exhausted";
const EXHAUSTED_IDENTITY_KEY = "cs_prev_exhausted_identity";
const QUOTA_SAMPLE_KEY = "cs_quota_sample";
const PENDING_POLL_MS = 1500;
const MAX_PENDING_POLLS = 8;
let refreshFlight = null, pendingTimer = null, pendingPolls = 0, refreshGeneration = 0;
let sampleLoaded = null, quotaSample = null;

const COLORS = {
  ok: "#10b981",
  warn: "#f59e0b",
  bad: "#ef4444",
  off: "#71717a",
};

async function apiGet(path, timeoutMs = 8000) {
  const controller = new AbortController();
  let timer;
  try {
    // Bound body reads too: abort alone need not settle a stalled response.
    return await Promise.race([(async () => {
      const response = await fetch(API + path, { signal: controller.signal, cache: "no-store" });
      const text = await response.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
      if (!response.ok) {
        const error = new Error(data.message || "HTTP " + response.status);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    })(), new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(new Error("本地服务请求超时"));
        controller.abort();
      }, timeoutMs);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

function identityKey(value = {}) {
  if (value.identity_error) return "";
  if (value.profile_id) return "profile:" + value.profile_id;
  const workspace = value.workspace_id || value.account_id;
  const user = value.user_id || String(value.email || "").toLowerCase();
  // Email alone cannot distinguish personal and Team workspaces.
  return workspace && user ? JSON.stringify([workspace, user]) : "";
}

function sameIdentity(left, right) {
  const key = identityKey(left);
  const a = left.workspace_id || left.account_id, b = right.workspace_id || right.account_id;
  return !!key && key === identityKey(right) && !(a && b && a !== b);
}

async function loadQuotaSample() {
  if (!sampleLoaded) sampleLoaded = chrome.storage.local.get({ [QUOTA_SAMPLE_KEY]: null })
    .then(value => { quotaSample = value[QUOTA_SAMPLE_KEY]; }, () => {});
  await sampleLoaded;
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
    await setBadge("?", COLORS.off, snapshot.quota_pending
      ? "Codex Switcher：服务在线，正在读取当前工作区额度"
      : "Codex Switcher：服务在线，额度读取失败（" + (snapshot.quota_error || "unknown") + "）");
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
    (snapshot.limit_reached ? "\n已用尽，点图标切换账号" : "") +
    (snapshot.quota_pending ? "\n正在刷新，显示上次结果" : snapshot.quota_stale ? "\n额度尚未更新，显示上次结果" : "") +
    (snapshot.quota_refresh_error ? "\n刷新失败：" + snapshot.quota_refresh_error : "") +
    (snapshot.quota_at ? "\n采样于 " + new Date(snapshot.quota_at).toLocaleString() : "");
  await setBadge(text, color, title);
}

async function maybeNotify(snapshot, generation) {
  // An unavailable or stale read neither announces exhaustion nor clears a
  // previous notification, which would announce it again on reconnection.
  if (generation !== refreshGeneration || !snapshot.online || !snapshot.logged_in || !snapshot.quota_ok ||
      snapshot.quota_pending || snapshot.quota_stale || snapshot.quota_refresh_error) return;
  const identity = identityKey(snapshot);
  if (!identity) return;
  const stored = await chrome.storage.local.get({ [EXHAUSTED_KEY]: false, [EXHAUSTED_IDENTITY_KEY]: "" });
  if (generation !== refreshGeneration) return;
  const was = stored[EXHAUSTED_IDENTITY_KEY] === identity && !!stored[EXHAUSTED_KEY];
  const now = !!(snapshot.online && snapshot.logged_in && snapshot.quota_ok && snapshot.limit_reached);
  if (now && !was) {
    const settings = await chrome.storage.local.get({ notify_on_exhausted: true });
    if (generation !== refreshGeneration) return;
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
  if (generation !== refreshGeneration) return;
  await chrome.storage.local.set({ [EXHAUSTED_KEY]: now, [EXHAUSTED_IDENTITY_KEY]: identity });
}

function snapshotFor(status, quota, error) {
  const auth = status.auth || {};
  const matches = quota && sameIdentity(auth, quota);
  const pending = !!(quota && (quota.pending || quota.refreshing)) || !!(quota && !matches);
  const known = matches && quota.ok ? quota
    : quotaSample && quotaSample.ok && sameIdentity(auth, quotaSample) ? quotaSample : null;
  return {
      ok: true,
      online: true,
      at: Date.now(),
      logged_in: !!auth.logged_in,
      profile_id: auth.profile_id || "",
      workspace_id: auth.workspace_id || auth.account_id || "",
      user_id: auth.user_id || "",
      email: auth.email || "",
      name: auth.name || "",
      plan: matches && quota.plan || auth.plan || "",
      accounts_total: (status.accounts && status.accounts.total) || 0,
      switching_enabled: !!(status.switching && status.switching.enabled),
      switching_message: (status.switching && status.switching.message) || "",
      quota_ok: !!(auth.logged_in && known),
      quota_error: !auth.logged_in ? "not_logged_in" : error || (matches ? quota.error : quota ? "workspace_changed" : "quota_unavailable") || null,
      quota_pending: !!auth.logged_in && pending,
      quota_stale: !!(known && (pending || error || !matches || !quota.ok || quota.stale || quota.refresh_error)),
      quota_refresh_error: error || (matches && quota.refresh_error && (quota.refresh_error.message || quota.refresh_error.error)) ||
        (matches && !quota.ok && !pending && (quota.message || quota.error)) || null,
      quota_at: known && known.fetched_at || null,
      limit_reached: !!(auth.logged_in && known && known.limit_reached),
      remaining_5h: auth.logged_in && known ? remainingPercent(known, "primary_window") : null,
      remaining_7d: auth.logged_in && known ? remainingPercent(known, "secondary_window") : null,
      used_5h: known && known.primary_window ? known.primary_window.used_percent : null,
      used_7d: known && known.secondary_window ? known.secondary_window.used_percent : null,
      reset_at_5h: known && known.primary_window ? known.primary_window.reset_at : null,
      reset_at_7d: known && known.secondary_window ? known.secondary_window.reset_at : null,
  };
}

async function publishSnapshot(snapshot, generation) {
  if (generation !== refreshGeneration) return snapshot;
  await chrome.storage.local.set({ [SNAPSHOT_KEY]: snapshot });
  if (generation !== refreshGeneration) return snapshot;
  await paint(snapshot);
  if (generation !== refreshGeneration) return snapshot;
  await maybeNotify(snapshot, generation);
  return snapshot;
}

async function refreshOnce(generation) {
  // Start both reads together and immediately handle failures on either.
  const statusRead = apiGet("/api/status").then(value => ({ value }), error => ({ error }));
  let quotaResult = null;
  const quotaRead = apiGet("/api/quota?background=1").then(value => { quotaResult = { value }; }, error => { quotaResult = { error }; });
  await loadQuotaSample();
  const statusResult = await statusRead;
  if (statusResult.error) {
    await quotaRead;
    return publishSnapshot({
      ok: false,
      online: false,
      at: Date.now(),
      error: statusResult.error.message || "offline",
      logged_in: false,
      quota_ok: false,
      remaining_5h: null,
      remaining_7d: null,
      limit_reached: false,
    }, generation);
  }
  const status = statusResult.value;
  if (!quotaResult) {
    const waiting = { ...(status.auth || {}), pending: true, ok: false };
    await publishSnapshot(snapshotFor(status, waiting), generation);
  }
  await quotaRead;
  const quota = quotaResult.value && quotaResult.value.quota;
  if (generation === refreshGeneration && quota && quota.ok && quota.fetched_at && sameIdentity(status.auth || {}, quota)) {
    if (JSON.stringify(quotaSample) !== JSON.stringify(quota)) {
      quotaSample = quota;
      await chrome.storage.local.set({ [QUOTA_SAMPLE_KEY]: quota });
    }
  }
  return publishSnapshot(snapshotFor(status, quota, quotaResult.error && quotaResult.error.message), generation);
}

function refresh(poll = false) {
  if (refreshFlight) return refreshFlight;
  if (pendingTimer !== null) clearTimeout(pendingTimer);
  pendingTimer = null;
  if (!poll) pendingPolls = 0;
  const generation = refreshGeneration;
  refreshFlight = refreshOnce(generation).then(snapshot => {
    if (generation === refreshGeneration && snapshot.online && snapshot.quota_pending && pendingPolls < MAX_PENDING_POLLS) {
      pendingTimer = setTimeout(() => {
        pendingTimer = null;
        pendingPolls++;
        refresh(true);
      }, PENDING_POLL_MS);
    }
    return snapshot;
  }).finally(() => {
    refreshFlight = null;
    if (generation !== refreshGeneration) refresh();
  });
  return refreshFlight;
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
    // A new sidebar sample may follow an account switch. Do not paint the
    // reads started before it; merge such pings into one follow-up refresh.
    refreshGeneration++;
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
