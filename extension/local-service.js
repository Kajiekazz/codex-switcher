// Shared loopback discovery for the worker and side panel. No credentials are
// sent to a candidate until its read-only health endpoint identifies Switcher.
(function (root) {
  "use strict";
  const DEFAULT_URL = "http://127.0.0.1:8787";
  const ADDRESS_KEY = "cs_service_address", CACHE_KEY = "cs_service_discovered_url";

  function normalize(value) {
    const match = /^http:\/\/127\.0\.0\.1:(\d{1,5})\/?$/.exec(String(value || "").trim());
    if (!match || Number(match[1]) < 1 || Number(match[1]) > 65535) {
      throw new Error("地址须为 http://127.0.0.1:端口（1–65535）");
    }
    return "http://127.0.0.1:" + Number(match[1]);
  }

  function createClient(options) {
    const transport = options.fetch, storage = options.storage;
    const now = options.now || Date.now;
    const later = options.setTimeout || setTimeout, cancel = options.clearTimeout || clearTimeout;
    let configured = "", cached = "", selected = "", loaded = null, flight = null;
    let failures = 0, retryAt = 0, generation = 0;
    const listeners = new Set();
    const safeAddress = value => { try { return value ? normalize(value) : ""; } catch (_) { return ""; } };
    const offline = () => new Error("未找到本地 Switcher 服务，请启动软件或检查连接设置");

    async function load() {
      if (!loaded) loaded = Promise.resolve(storage.get({ [ADDRESS_KEY]: "", [CACHE_KEY]: "" }))
        .then(value => { configured = safeAddress(value[ADDRESS_KEY]); cached = safeAddress(value[CACHE_KEY]); }, () => {});
      await loaded;
    }

    async function probe(address) {
      const controller = new AbortController();
      let timer;
      try {
        return await Promise.race([(async () => {
          const response = await transport(address + "/api/health", {
            method: "GET", cache: "no-store", credentials: "omit", redirect: "error", signal: controller.signal,
          });
          if (!response.ok) return false;
          const value = JSON.parse(await response.text());
          return value.ok === true && (value.service === "codex-switcher" ||
            (Number.isInteger(value.service_pid) && value.service_pid > 0 &&
             Number.isInteger(value.session_api_version) && value.session_api_version > 0));
        })(), new Promise(resolve => {
          timer = later(() => { resolve(false); controller.abort(); }, 700);
        })]);
      } catch (_) { return false; }
      finally { cancel(timer); }
    }

    async function resolve() {
      await load();
      if (selected) return selected;
      if (flight) return flight;
      if (now() < retryAt) throw offline();
      const stamp = generation;
      const pending = (async () => {
        const candidates = configured ? [configured] : [...new Set([
          cached, ...Array.from({ length: 11 }, (_, offset) => "http://127.0.0.1:" + (8787 + offset)),
        ].filter(Boolean))];
        let found = await probe(candidates[0]) ? candidates[0] : "";
        for (let index = 1; !found && index < candidates.length; index += 3) {
          const batch = candidates.slice(index, index + 3);
          const results = await Promise.all(batch.map(probe));
          found = batch[results.indexOf(true)] || "";
        }
        if (stamp !== generation) throw offline();
        if (!found) {
          retryAt = now() + Math.min(60000, 5000 * 2 ** failures++);
          throw offline();
        }
        selected = cached = found;
        failures = 0; retryAt = 0;
        await storage.set({ [CACHE_KEY]: found }).catch(() => {});
        if (stamp !== generation) throw offline();
        for (const listener of listeners) listener(found);
        return found;
      })();
      flight = pending;
      try { return await pending; }
      finally { if (flight === pending) flight = null; }
    }

    function invalidate(address) {
      if (address === selected) selected = "";
    }

    async function fetchLocal(path, init = {}) {
      if (!/^\/api\//.test(path)) throw new Error("无效的本地服务接口");
      const address = await resolve(), stamp = generation;
      const method = (init.method || "GET").toUpperCase();
      if (method !== "GET" && method !== "HEAD" && !await probe(address)) {
        invalidate(address);
        // Nothing has been submitted yet. A subsequent explicit action may
        // reconnect; never send a mutation to an unverified or changed target.
        throw offline();
      }
      if (stamp !== generation || selected !== address) throw offline();
      if (init.signal && init.signal.aborted) {
        const error = new Error("请求已取消"); error.name = "AbortError"; throw error;
      }
      try {
        const response = await transport(address + path, { ...init, credentials: "omit", redirect: "error" });
        if (!response.ok && !await probe(address)) {
          invalidate(address);
          throw offline();
        }
        return response;
      } catch (error) {
        if (!init.signal || !init.signal.aborted) invalidate(address);
        // A lost response does not establish whether a mutation was applied.
        // The caller receives the error; this layer never replays it.
        throw error;
      }
    }

    async function setAddress(value) {
      const address = String(value || "").trim() ? normalize(value) : "";
      await load();
      generation++; configured = address; selected = ""; cached = "";
      flight = null; failures = 0; retryAt = 0;
      await storage.set({ [ADDRESS_KEY]: address, [CACHE_KEY]: "" });
      return resolve();
    }

    if (options.changes) options.changes.addListener((changes, area) => {
      if (area !== "local" || !changes[ADDRESS_KEY]) return;
      const address = safeAddress(changes[ADDRESS_KEY].newValue);
      if (address === configured) return;
      generation++; configured = address; selected = ""; cached = "";
      flight = null; failures = 0; retryAt = 0;
    });

    return { resolve, fetch: fetchLocal, setAddress, normalize,
      connectionFailed: () => { selected = ""; },
      peek: () => selected || configured || cached || DEFAULT_URL,
      settings: async () => { await load(); return { address: configured, current: selected || cached }; },
      onChange: listener => { listeners.add(listener); return () => listeners.delete(listener); } };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { createClient, normalize };
  else root.CodexLocalService = createClient({ fetch: (...args) => fetch(...args),
    storage: chrome.storage.local, changes: chrome.storage.onChanged });
})(globalThis);
