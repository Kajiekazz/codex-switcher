// ==UserScript==
// @name         Codex 切号助手（本地服务）
// @namespace    https://local.codex-switcher
// @version      0.4.0
// @description  连接本地 Codex Switcher 服务：查看当前账号与额度、导入账号、切换账号（需显式启用）、打开登录助手；登录助手默认禁用。
// @match        https://chatgpt.com/*
// @match        https://auth.openai.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const API = "http://127.0.0.1:8787";
  const REFRESH_MS = 15000;

  function gmRequest(method, path, body, timeoutMs) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: API + path,
        headers: body ? { "Content-Type": "application/json" } : {},
        data: body ? JSON.stringify(body) : undefined,
        timeout: timeoutMs || 8000,
        onload(res) {
          let data = {};
          try {
            data = JSON.parse(res.responseText || "{}");
          } catch (_) {}
          if (res.status >= 200 && res.status < 300) {
            resolve(data);
          } else {
            const err = new Error(data.message || "HTTP " + res.status);
            err.data = data;
            reject(err);
          }
        },
        onerror() {
          reject(new Error("本地服务未启动"));
        },
        ontimeout() {
          reject(new Error("本地服务超时"));
        },
      });
    });
  }

  const gmGet = (path) => gmRequest("GET", path);
  const gmPost = (path, body, timeoutMs) => gmRequest("POST", path, body, timeoutMs);

  const host = document.createElement("div");
  host.id = "codex-switcher-root";
  document.documentElement.appendChild(host);
  const shadow = host.attachShadow({ mode: "open" });

  shadow.innerHTML = `
    <style>
      .panel {
        position: fixed;
        right: 16px;
        bottom: 16px;
        width: 320px;
        max-width: calc(100vw - 24px);
        z-index: 2147483647;
        background: #fafafa;
        color: #18181b;
        border: 1px solid rgba(24, 24, 27, .08);
        border-radius: 18px;
        box-shadow: 0 18px 50px rgba(15, 23, 42, .18);
        font-family: "Inter", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif;
        font-size: 13px;
        line-height: 1.45;
        overflow: hidden;
        user-select: none;
      }
      .head {
        display: flex; align-items: center; justify-content: space-between;
        padding: 12px 12px 12px 14px;
        background: #fafafa;
        border-bottom: 1px solid rgba(24, 24, 27, .06);
        cursor: move;
      }
      .title { display: flex; align-items: center; gap: 9px; font-weight: 600; font-size: 13px; letter-spacing: -.01em; }
      .logo {
        width: 22px; height: 22px; border-radius: 7px;
        display: inline-flex; align-items: center; justify-content: center;
        background: #18181b; color: #fafafa; font-size: 9px; font-weight: 700;
      }
      .icon {
        appearance: none; border: 0; background: transparent; color: #71717a;
        font-size: 16px; line-height: 1; cursor: pointer; padding: 3px 7px;
        border-radius: 8px;
      }
      .icon:hover { background: #f4f4f5; color: #18181b; }
      .body { padding: 14px; }
      .status { display: flex; align-items: center; gap: 7px; color: #71717a; font-size: 12px; }
      .dot { width: 7px; height: 7px; border-radius: 50%; background: #a1a1aa; flex: none; }
      .dot.online { background: #10b981; box-shadow: 0 0 0 3px rgba(16, 185, 129, .14); }
      .dot.offline { background: #ef4444; box-shadow: 0 0 0 3px rgba(239, 68, 68, .14); }
      .account {
        margin-top: 10px; font-size: 14px; font-weight: 600; color: #18181b;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .badge {
        display: inline-block; margin-top: 8px; padding: 3px 9px;
        border-radius: 999px; font-size: 11px;
        color: #b45309; background: rgba(245, 158, 11, .10); border: 1px solid rgba(245, 158, 11, .18);
      }
      .badge.ok { color: #047857; background: rgba(16, 185, 129, .10); border-color: rgba(16, 185, 129, .18); }
      .pool { margin-top: 9px; color: #71717a; font-size: 12px; }
      .quota { margin-top: 5px; color: #a1a1aa; font-size: 11px; line-height: 1.5; }
      .actions { display: flex; gap: 7px; margin-top: 12px; flex-wrap: wrap; }
      button {
        appearance: none; border: 1px solid rgba(24, 24, 27, .08); background: #ffffff;
        color: #18181b; border-radius: 9px; padding: 6px 10px;
        font: inherit; font-size: 11.5px; font-weight: 500; cursor: pointer;
        transition: background .15s ease, transform .15s ease;
      }
      button:hover { background: #f4f4f5; transform: translateY(-1px); }
      button.primary { border-color: #18181b; background: #18181b; color: #fafafa; }
      button.primary:hover { background: #27272a; }
      button:disabled { opacity: .4; cursor: not-allowed; transform: none; }
      .import { margin-top: 10px; }
      .switchBox { margin-top: 10px; }
      select {
        width: 100%; box-sizing: border-box;
        background: #ffffff; color: #18181b; border: 1px solid rgba(24, 24, 27, .08);
        border-radius: 9px; padding: 7px; font: 12px/1.4 "Inter", "Noto Sans SC", system-ui, -apple-system, "Segoe UI", sans-serif;
      }
      .check {
        display: flex; align-items: center; gap: 7px;
        margin-top: 8px; color: #71717a; font-size: 12px;
      }
      .check input { accent-color: #18181b; }
      textarea {
        width: 100%; min-height: 96px; resize: vertical; box-sizing: border-box;
        background: #ffffff; color: #18181b; border: 1px solid rgba(24, 24, 27, .08);
        border-radius: 9px; padding: 8px;
        font: 11px/1.5 ui-monospace, Consolas, monospace;
      }
      .row { display: flex; gap: 8px; margin-top: 8px; }
      .msg { margin-top: 7px; font-size: 12px; color: #71717a; white-space: pre-wrap; }
      .msg.ok { color: #047857; }
      .msg.bad { color: #b91c1c; }
      .foot {
        margin-top: 11px; padding-top: 9px; border-top: 1px solid rgba(24, 24, 27, .06);
        color: #a1a1aa; font-size: 10.5px; line-height: 1.6;
      }
    </style>
    <div class="panel" id="panel">
      <div class="head" id="head">
        <div class="title"><span class="logo">CS</span><span>Codex 切号助手</span></div>
        <button class="icon" id="collapseBtn" title="折叠">−</button>
      </div>
      <div class="body" id="body">
        <div class="status"><span class="dot" id="dot"></span><span id="conn">连接中…</span></div>
        <div class="account" id="account">未登录</div>
        <div class="badge" id="switchBadge">切号已禁用</div>
        <div class="pool" id="pool">账号池：—</div>
        <div class="quota" id="quota">额度：—</div>
        <div class="actions">
          <button id="refreshBtn">刷新</button>
          <button id="importToggle">导入账号</button>
          <button id="switchToggle" disabled>切号</button>
          <button id="dashboardBtn">登录助手</button>
        </div>
        <div class="switchBox" id="switchBox" hidden>
          <select id="switchTarget"><option value="">选择目标账号…</option></select>
          <label class="check"><input type="checkbox" id="switchConfirm"> 确认切换并接受自动回滚</label>
          <div class="row">
            <button class="primary" id="switchBtn" disabled>切换账号</button>
            <button id="switchCancel">取消</button>
          </div>
          <div class="msg" id="switchMsg"></div>
        </div>
        <div class="import" id="importBox" hidden>
          <textarea id="importText" spellcheck="false"
            placeholder="email-----password---2fa&#10;或 email|password|2fa"></textarea>
          <div class="row">
            <button class="primary" id="importBtn">导入</button>
            <button id="importCancel">取消</button>
          </div>
          <div class="msg" id="importMsg"></div>
        </div>
        <div class="foot">127.0.0.1 · v0.4 · 切号/自动切号需显式启用 · 登录助手默认禁用</div>
      </div>
    </div>
  `;

  const $ = (sel) => shadow.querySelector(sel);
  const panel = $("#panel");
  const head = $("#head");
  const body = $("#body");
  const dot = $("#dot");
  const conn = $("#conn");
  const account = $("#account");
  const switchBadge = $("#switchBadge");
  const pool = $("#pool");
  const quota = $("#quota");
  const importBox = $("#importBox");
  const importText = $("#importText");
  const importMsg = $("#importMsg");
  const switchBox = $("#switchBox");
  const switchTarget = $("#switchTarget");
  const switchConfirm = $("#switchConfirm");
  const switchBtn = $("#switchBtn");
  const switchMsg = $("#switchMsg");
  let switchState = { switching: {}, current: null, snapshots: [] };

  // Restore panel position and collapsed state.
  const savedPos = GM_getValue("panel_pos", null);
  if (savedPos && typeof savedPos.left === "number") {
    panel.style.left = savedPos.left + "px";
    panel.style.top = savedPos.top + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
  }
  let collapsed = GM_getValue("panel_collapsed", false);
  function applyCollapsed() {
    body.hidden = collapsed;
    $("#collapseBtn").textContent = collapsed ? "+" : "−";
    $("#collapseBtn").title = collapsed ? "展开" : "折叠";
  }
  applyCollapsed();

  $("#collapseBtn").addEventListener("click", (event) => {
    event.stopPropagation();
    collapsed = !collapsed;
    GM_setValue("panel_collapsed", collapsed);
    applyCollapsed();
  });

  // Drag by header.
  let dragging = false;
  let dragStart = null;
  head.addEventListener("mousedown", (event) => {
    if (event.target.closest("button")) return;
    dragging = true;
    const rect = panel.getBoundingClientRect();
    dragStart = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
    event.preventDefault();
  });
  window.addEventListener("mousemove", (event) => {
    if (!dragging || !dragStart) return;
    const nextLeft = Math.max(8, Math.min(window.innerWidth - panel.offsetWidth - 8,
      dragStart.left + event.clientX - dragStart.x));
    const nextTop = Math.max(8, Math.min(window.innerHeight - 40,
      dragStart.top + event.clientY - dragStart.y));
    panel.style.left = nextLeft + "px";
    panel.style.top = nextTop + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    const rect = panel.getBoundingClientRect();
    GM_setValue("panel_pos", { left: rect.left, top: rect.top });
  });

  function setConn(ok, text) {
    dot.classList.toggle("online", ok);
    dot.classList.toggle("offline", !ok);
    conn.textContent = text;
  }

  function renderStatus(status) {
    setConn(true, "本地服务在线");
    const auth = status.auth || {};
    if (auth.logged_in) {
      const plan = auth.plan ? " · " + auth.plan : "";
      const who = auth.name ? auth.name + " · " : "";
      account.textContent = who + (auth.email || "未知账号") + plan;
      account.title = auth.account_id || "";
    } else {
      account.textContent = "未登录（auth.json 不存在）";
      account.title = "";
    }
    const sw = status.switching || {};
    switchBadge.textContent = sw.enabled ? "切号已启用" : "切号已禁用";
    switchBadge.className = "badge" + (sw.enabled ? " ok" : "");
    switchBadge.title = sw.message || "";
    const total = status.accounts ? status.accounts.total : 0;
    pool.textContent = "账号池：" + total + " 个";
  }

  function updateSwitchButton() {
    const enabled = !!(switchState.switching && switchState.switching.enabled);
    switchBtn.disabled = !enabled || !switchTarget.value || !switchConfirm.checked;
  }

  function renderSwitch(data) {
    switchState = data || { switching: {}, current: null, snapshots: [] };
    const sw = switchState.switching || {};
    const current = switchState.current || {};
    const snapshots = (switchState.snapshots || []).filter((s) => s.email && s.email !== current.email);
    const enabled = !!sw.enabled;
    $("#switchToggle").disabled = !enabled;
    $("#switchToggle").title = sw.message || "";
    const previous = switchTarget.value;
    switchTarget.textContent = "";
    if (!snapshots.length) {
      switchTarget.appendChild(new Option(enabled ? "没有可切换快照" : "切号未启用", ""));
    } else {
      snapshots.forEach((s) => {
        const label = (s.name ? s.name + " · " : "") + (s.email_masked || s.email) + (s.plan ? " · " + s.plan : "") +
          (s.expired ? " · token 过期" : "");
        switchTarget.appendChild(new Option(label, s.email));
      });
      if (snapshots.some((s) => s.email === previous)) switchTarget.value = previous;
    }
    switchBadge.textContent = enabled ? "切号已启用" : "切号已禁用";
    switchBadge.className = "badge" + (enabled ? " ok" : "");
    switchBadge.title = sw.message || "";
    updateSwitchButton();
  }

  async function refreshSnapshots() {
    try {
      const data = await gmGet("/api/snapshots");
      renderSwitch(data);
    } catch (_) {}
  }

  async function doSwitch() {
    const email = switchTarget.value;
    if (!email) {
      switchMsg.className = "msg bad";
      switchMsg.textContent = "先选择目标账号";
      return;
    }
    if (!switchConfirm.checked) {
      switchMsg.className = "msg bad";
      switchMsg.textContent = "先勾选确认";
      return;
    }
    if (!window.confirm(
      "确认把 ~/.codex/auth.json 切换到：\n" + email +
      "\n\n切换前会加密备份当前 auth；写入后会重新校验；失败会自动回滚。"
    )) {
      return;
    }
    switchMsg.className = "msg";
    switchMsg.textContent = "切换中…";
    switchBtn.disabled = true;
    try {
      const data = await gmPost("/api/switch", { email, confirm: true }, 30000);
      const sw = data.switch || {};
      switchMsg.className = "msg ok";
      switchMsg.textContent =
        "已切换：" + (sw.previous_email || "—") + " → " + sw.target_email +
        (sw.warning ? "\n" + sw.warning : "") +
        "\n备份：" + (sw.backup_path || "—");
      switchConfirm.checked = false;
      await refresh();
      await refreshSnapshots();
    } catch (err) {
      switchMsg.className = "msg bad";
      switchMsg.textContent = "切换失败：" + (err.message || "未知错误") +
        (err.data && err.data.detail ? "\n" + err.data.detail : "");
    } finally {
      updateSwitchButton();
    }
  }

  async function refreshQuota() {
    try {
      const data = await gmGet("/api/quota");
      const q = data.quota || {};
      if (!q.ok) {
        quota.textContent = "额度：不可用" + (q.error ? "（" + q.error + "）" : "");
        quota.title = q.message || "";
        return;
      }
      const p = q.primary_window || null;
      const s = q.secondary_window || null;
      const parts = [];
      if (p) parts.push("5h " + (p.used_percent ?? "?") + "% 已用");
      if (s) parts.push("7d " + (s.used_percent ?? "?") + "% 已用");
      let text = "额度：" + (parts.length ? parts.join(" · ") : "未知");
      if (q.limit_reached) text += " · 已用尽";
      quota.textContent = text;
      quota.title = p && p.reset_at
        ? "5h 重置：" + new Date(p.reset_at * 1000).toLocaleString()
        : "";
    } catch (_) {
      quota.textContent = "额度：—";
    }
  }

  async function refresh() {
    try {
      const status = await gmGet("/api/status");
      renderStatus(status);
      refreshQuota();
      refreshSnapshots();
    } catch (err) {
      setConn(false, err.message || "本地服务离线");
      account.textContent = "先运行 python service.py";
      account.title = "";
      pool.textContent = "账号池：—";
      quota.textContent = "额度：—";
    }
  }

  async function doImport() {
    const text = importText.value;
    if (!text.trim()) {
      importMsg.className = "msg bad";
      importMsg.textContent = "先粘贴账号。";
      return;
    }
    importMsg.className = "msg";
    importMsg.textContent = "导入中…";
    try {
      const result = await gmPost("/api/accounts/import", { text });
      importMsg.className = "msg ok";
      importMsg.textContent =
        "导入完成：新增 " + result.inserted + "，更新 " + result.updated +
        (result.errors && result.errors.length ? "，错误 " + result.errors.length : "");
      await refresh();
    } catch (err) {
      importMsg.className = "msg bad";
      importMsg.textContent = "导入失败：" + (err.message || "未知错误");
    }
  }

  $("#refreshBtn").addEventListener("click", refresh);
  $("#importToggle").addEventListener("click", () => {
    importBox.hidden = !importBox.hidden;
    if (!importBox.hidden) importText.focus();
  });
  $("#importCancel").addEventListener("click", () => {
    importBox.hidden = true;
    importMsg.textContent = "";
  });
  $("#importBtn").addEventListener("click", doImport);
  $("#switchToggle").addEventListener("click", () => {
    switchBox.hidden = !switchBox.hidden;
    if (!switchBox.hidden) refreshSnapshots();
  });
  $("#switchCancel").addEventListener("click", () => {
    switchBox.hidden = true;
    switchMsg.textContent = "";
  });
  $("#switchBtn").addEventListener("click", doSwitch);
  switchTarget.addEventListener("change", updateSwitchButton);
  switchConfirm.addEventListener("change", updateSwitchButton);
  $("#dashboardBtn").addEventListener("click", () => {
    window.open("http://127.0.0.1:8787/#login", "_blank", "noopener");
  });

  // Keep page shortcuts / React handlers from seeing panel keystrokes.
  panel.addEventListener("keydown", (event) => event.stopPropagation());
  panel.addEventListener("click", (event) => event.stopPropagation());

  refresh();
  setInterval(refresh, REFRESH_MS);
  window.addEventListener("focus", refresh);
})();
