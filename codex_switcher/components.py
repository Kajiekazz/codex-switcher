"""Download only the pinned Playwright browser when automatic login needs it."""
from __future__ import annotations

import json
import os
import queue
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import config


class ComponentError(RuntimeError):
    pass


_EVENT = "SWITCHER_COMPONENT "
_CATALOG_SCRIPT = r"""
const {registry} = require(process.argv[1]).registry;
const fs = require('fs');
const path = require('path');
const overrides = ['PLAYWRIGHT_DOWNLOAD_HOST', 'PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST'];
const custom = overrides.some(k => process.env[k] || process.env['npm_config_' + k.toLowerCase()] || process.env['npm_package_config_' + k.toLowerCase()]);
console.log(JSON.stringify({custom, executables: registry.resolveBrowsers(['chromium'], {shell:'no'}).map(e => ({name:e.name, urls:e.downloadURLs, directory:e.directory, executable:e.executablePath(), version:e.browserVersion, revision:e.revision,
 installed:!!e.directory && fs.existsSync(path.join(e.directory,'INSTALLATION_COMPLETE')) && !!e.executablePath() && fs.existsSync(e.executablePath())}))}));
"""

# Use Playwright's own installer for extraction, locking and installation markers.
# Hook only its owned download workers; no running browser or other Node process is touched.
_INSTALL_SCRIPT = r"""
const cp = require('child_process');
const routes = JSON.parse(process.env.SWITCHER_COMPONENT_ROUTES);
const emit = value => console.log('SWITCHER_COMPONENT ' + JSON.stringify(value));
const children = new Set();
const attempts = new Map();
let stopping = false;
function stop() {
  if (stopping) return;
  stopping = true;
  for (const child of children) child.kill();
  const finish = () => { if (!children.size) process.exit(130); };
  for (const child of children) child.once('close', finish);
  finish();
}
process.stdin.setEncoding('utf8');
process.stdin.on('data', data => { if (data.includes('cancel')) stop(); });
process.stdin.on('end', stop);
const fork = cp.fork;
cp.fork = function(...args) {
  if (stopping) throw new Error('download cancelled');
  const child = fork.apply(this, args);
  children.add(child);
  child.once('close', () => children.delete(child));
  const send = child.send.bind(child);
  let began = 0;
  child.send = function(message, ...rest) {
    if (message?.method === 'download') {
      const url = message.params.url;
      const component = Object.keys(routes).find(name => routes[name].includes(url));
      if (!component) throw new Error('Unprobed download URL rejected');
      const attempt = (attempts.get(component) || 0) + 1;
      attempts.set(component, attempt);
      began = Date.now();
      emit({kind:'download', component, url, attempt, attempts:5});
    }
    return send(message, ...rest);
  };
  child.on('message', message => {
    if (message?.method === 'progress') {
      const {done, total} = message.params;
      emit({kind:'progress', downloaded_bytes:done, total_bytes:total || null,
            speed_bps:began && Date.now() > began ? done / ((Date.now()-began)/1000) : null});
    }
  });
  return child;
};
(async () => {
  const {registry} = require(process.argv[1]).registry;
  registry._downloadURLs = descriptor => {
    if (!routes[descriptor.name]?.length) throw new Error('Missing probed download route');
    return routes[descriptor.name];
  };
  await registry.install(registry.resolveBrowsers(['chromium'], {shell:'no'}), {gc:false});
  emit({kind:'verifying'});
  process.exit(0);
})().catch(() => { emit({kind:'failed'}); stop(); process.exitCode = 1; });
"""


def _driver():
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    return str(node), str(Path(cli).parent / "lib" / "coreBundle.js"), {
        **get_driver_env(), "PLAYWRIGHT_BROWSERS_PATH": str(browser_root()), "CI": "1",
        "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "20000"}


def _catalog() -> dict:
    node, bundle, env = _driver()
    result = subprocess.run([node, "-e", _CATALOG_SCRIPT, bundle], env=env,
                            capture_output=True, text=True, encoding="utf8", timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ComponentError("无法读取浏览器组件清单，请重新安装或更新 Switcher")
    return json.loads(result.stdout)


def _source(url: str, *, custom=False) -> dict:
    parsed = urlsplit(url)
    official_hosts = {"storage.googleapis.com", "playwright.download.prss.microsoft.com", "cdn.playwright.dev"}
    if custom or parsed.hostname not in official_hosts:
        source_id, label = "configured", "环境配置下载源"
    elif parsed.hostname == "storage.googleapis.com":
        source_id, label = "chrome-for-testing", "Chrome for Testing 官方源"
    elif parsed.hostname == "playwright.download.prss.microsoft.com":
        source_id, label = "playwright-microsoft", "Playwright Microsoft CDN"
    elif parsed.path.startswith("/dbazure/"):
        source_id, label = "playwright-azure", "Playwright Azure CDN"
    else:
        source_id, label = "playwright", "Playwright 官方源"
    return {"id": source_id, "label": label, "host": parsed.hostname or ""}


_PROBE_SCRIPT = r"""
const {httpRequest} = require(process.argv[1]).utils;
const began = performance.now();
const limit = 131072;
let bytes = 0, chunks = [], latency = null, finished = false;
function fail() { if (!finished) { finished = true; process.exit(1); } }
httpRequest({url:process.env.SWITCHER_COMPONENT_PROBE, method:'GET',
  headers:{Range:'bytes=0-131071', 'Accept-Encoding':'identity'}, socketTimeout:6000}, response => {
  if (![200,206].includes(response.statusCode) ||
      (response.statusCode === 206 && !String(response.headers['content-range']).startsWith('bytes 0-'))) return fail();
  function finish() {
    if (finished) return;
    const data = Buffer.concat(chunks);
    if (data.length < 4 || !['504b0304','504b0506','504b0708'].includes(data.subarray(0,4).toString('hex'))) return fail();
    finished = true;
    const elapsed = (performance.now()-began)/1000;
    console.log(JSON.stringify({status:'available',latency_ms:latency,speed_bps:elapsed>0?bytes/elapsed:null}));
    response.destroy();
    process.exit(0);
  }
  response.on('data', chunk => {
    if (latency === null) latency = performance.now()-began;
    const sample = chunk.subarray(0, limit-bytes);
    chunks.push(sample); bytes += sample.length;
    if (bytes >= limit) finish();
  });
  response.on('end', finish);
  response.on('error', fail);
}, fail);
"""


def _probe(url: str, cancel: threading.Event, timeout=8.0) -> dict:
    """Sample the actual ZIP through the installer's own proxy/TLS/redirect implementation."""
    if cancel.is_set():
        raise ComponentError("测速已停止")
    node, bundle, env = _driver()
    env["SWITCHER_COMPONENT_PROBE"] = url
    proc = subprocess.Popen([node, "-e", _PROBE_SCRIPT, bundle], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf8",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise ComponentError("测速已停止或超时")
            try:
                output, _ = proc.communicate(timeout=min(.2, max(.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode:
            raise ComponentError("下载源未返回有效 ZIP 安装包")
        return json.loads(output)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=2)


def browser_root() -> Path:
    return config.data_dir() / "components" / "browsers"


def browser_executable() -> Path | None:
    try:
        import playwright
        manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        browser = next(x for x in json.loads(manifest.read_text(encoding="utf8"))["browsers"]
                       if x["name"] == "chromium")
        folder = browser_root() / ("chromium-" + browser["revision"])
        for name in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe",
                     "chrome-linux64/chrome", "chrome-linux/chrome"):
            path = folder / name
            if path.is_file() and (folder / "INSTALLATION_COMPLETE").is_file():
                return path
    except (ImportError, OSError, ValueError, KeyError, StopIteration):
        pass
    return None


def managed_browser_required() -> bool:
    return bool(getattr(sys, "frozen", False) or browser_executable())


def browser_launch_options() -> dict:
    if managed_browser_required():
        executable = browser_executable()
        if not executable:
            raise ComponentError("登录浏览器尚未准备，请在使用引导中下载后重试")
        return {"executable_path": str(executable)}
    # Preserve the source distribution's existing system-Chrome workflow.
    return {"channel": "chrome"}


class BrowserComponents:
    def __init__(self, process_factory=subprocess.Popen, executable=browser_executable, timeout=900,
                 *, catalog_loader=_catalog, probe=_probe):
        self._lock = threading.RLock()
        self._done, self._cancel = threading.Event(), threading.Event()
        self._probe_gate = threading.BoundedSemaphore(3)
        self._proc = None
        self._active = False
        self._state, self._phase = "missing", None
        self._message = "首次自动登录时下载专用浏览器；手动授权无需下载"
        self._progress = None
        self._factory, self._executable, self.timeout = process_factory, executable, timeout
        self._catalog_loader, self._probe = catalog_loader, probe
        self._catalog_data = None
        self._catalog_error = None
        self._routes, self._sources = {}, []
        self._source = None
        self._attempt, self._attempts = 0, 0
        self._downloaded_bytes = self._total_bytes = self._speed_bps = None

    def _discover(self):
        # Local registry discovery is cached, including failures; GET never probes the network.
        if self._catalog_data is not None or self._catalog_error:
            return
        try:
            catalog = self._catalog_loader()
            custom = bool(catalog.get("custom"))
            routes = {}
            for item in catalog["executables"]:
                urls = list(dict.fromkeys(item["urls"]))
                if item["name"] == "chromium" and not custom:
                    for url in list(urls):
                        parsed = urlsplit(url)
                        if parsed.hostname == "cdn.playwright.dev" and parsed.path.startswith("/builds/cft/"):
                            urls.append("https://storage.googleapis.com/chrome-for-testing-public/" +
                                        parsed.path.removeprefix("/builds/cft/"))
                routes[item["name"]] = urls
            if not routes.get("chromium"):
                raise ComponentError("组件清单缺少 Chromium 下载地址")
            self._catalog_data, self._routes = catalog, routes
            self._sources = [{**_source(url, custom=custom), "status": "pending",
                              "latency_ms": None, "speed_bps": None}
                             for url in routes["chromium"]]
        except Exception:
            self._catalog_error = "无法读取浏览器组件清单，请重新安装或更新 Switcher"

    def status(self) -> dict:
        with self._lock:
            ready = self._executable() is not None and not self._active and self._state != "failed"
            if not ready:
                self._discover()
            return {"state": "ready" if ready else self._state, "ready": ready,
                    "phase": "ready" if ready else self._phase, "progress": self._progress,
                    "message": "登录浏览器已准备" if ready else self._message,
                    "source": dict(self._source) if self._source else None,
                    "sources": [dict(source) for source in self._sources],
                    "attempt": self._attempt, "attempts": self._attempts,
                    "downloaded_bytes": self._downloaded_bytes, "total_bytes": self._total_bytes,
                    "speed_bps": self._speed_bps}

    def start(self, *, retry=True, source_id="auto") -> dict:
        with self._lock:
            if source_id != "auto":
                self._discover()
                if source_id not in {source["id"] for source in self._sources}:
                    raise ValueError("未知的浏览器下载源")
            if self._active or self.status()["ready"]:
                return self.status()
            if not retry and self._state == "failed":
                return self.status()
            if self._catalog_error:
                self._catalog_error = None
                self._discover()
            self._state, self._phase, self._message = "installing", "probing", "正在测试下载源…"
            self._progress, self._source = None, None
            self._attempt = self._attempts = 0
            self._downloaded_bytes = self._total_bytes = self._speed_bps = None
            for source in self._sources:
                source.update(status="pending", latency_ms=None, speed_bps=None)
                source.pop("error", None)
            self._done.clear()
            self._cancel = threading.Event()
            self._active = True
            threading.Thread(target=self._install, args=(source_id,), name="browser-download", daemon=True).start()
            return self.status()

    def _probe_routes(self, source_id):
        results = {}
        installed = set()
        for item in self._catalog_data["executables"]:
            if item.get("directory") and item.get("executable"):
                exists = (Path(item["directory"]) / "INSTALLATION_COMPLETE").is_file() and Path(item["executable"]).is_file()
            else:
                exists = item.get("installed", False)
            if exists:
                installed.add(item["name"])
        urls = list(dict.fromkeys(url for name, values in self._routes.items() if name not in installed for url in values))
        tasks, completed = queue.Queue(), queue.Queue()
        cancel = self._cancel
        for url in urls:
            tasks.put(url)

        def worker():
            while not cancel.is_set():
                try:
                    url = tasks.get_nowait()
                except queue.Empty:
                    return
                while not cancel.is_set():
                    if self._probe_gate.acquire(timeout=.1):
                        break
                else:
                    return
                try:
                    result = self._probe(url, cancel)
                except Exception:
                    result = {"status": "failed", "error": "连接超时或未返回有效安装包",
                              "latency_ms": None, "speed_bps": None}
                finally:
                    self._probe_gate.release()
                completed.put((url, result))

        with self._lock:
            for source in self._sources:
                source["status"] = "testing"
        # Daemon workers and bounded result waits avoid waiting forever on OS DNS resolution.
        # A shared semaphore also caps probes across an immediate retry after cancellation.
        for _ in range(min(3, len(urls))):
            threading.Thread(target=worker, name="browser-source", daemon=True).start()
        while len(results) < len(urls):
            if cancel.is_set():
                raise ComponentError("下载已停止或超时，可点击重试")
            try:
                url, result = completed.get(timeout=.1)
            except queue.Empty:
                continue
            results[url] = result
            with self._lock:
                for index, candidate in enumerate(self._routes["chromium"]):
                    if candidate == url:
                        self._sources[index].update(result)
        ranked = {}
        for name, candidates in self._routes.items():
            if name in installed:
                # The original registry will reuse this exact revision without downloading it.
                ranked[name] = candidates
                continue
            available = [url for url in candidates if results[url]["status"] == "available"]
            available.sort(key=lambda url: (-(results[url].get("speed_bps") or 0),
                                            results[url].get("latency_ms") or float("inf")))
            if name == "chromium" and source_id != "auto":
                available.sort(key=lambda url: _source(url, custom=self._catalog_data.get("custom"))["id"] != source_id)
            if not available:
                raise ComponentError("没有可用的下载源（" + name + "），请检查网络或代理后重试")
            ranked[name] = available
        return ranked

    def _install(self, source_id):
        cancel = self._cancel
        timer = threading.Timer(self.timeout, lambda: self._cancel_download(cancel))
        timer.daemon = True
        timer.start()
        try:
            if self._catalog_error:
                raise ComponentError(self._catalog_error)
            routes = self._probe_routes(source_id)
            if self._cancel.is_set():
                raise ComponentError("下载已停止或超时，可点击重试")
            node, bundle, env = _driver()
            env["SWITCHER_COMPONENT_ROUTES"] = json.dumps(routes)
            browser_root().mkdir(parents=True, exist_ok=True)
            proc = self._factory([node, "-e", _INSTALL_SCRIPT, bundle], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                self._proc = proc
                self._phase, self._message = "downloading", "正在下载登录浏览器…"
            if self._cancel.is_set():
                self._stop_process(proc)
            lines = []
            for raw in proc.stdout:
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).strip()
                if not line:
                    continue
                if line.startswith(_EVENT):
                    try:
                        self._event(json.loads(line[len(_EVENT):]))
                    except (ValueError, TypeError, KeyError):
                        pass
                    continue
                # Driver output can contain configured proxy credentials; never expose raw URLs.
                line = re.sub(r"https?://\S+", "[下载源]", line)
                lines = (lines + [line])[-5:]
            code = proc.wait()
            with self._lock:
                self._phase = "verifying"
            if self._cancel.is_set():
                raise ComponentError("下载已停止或超时，可点击重试；已下载的其他组件会复用")
            if code or not self._executable():
                raise ComponentError("下载未完成，请检查网络后重试。" + " ".join(lines)[-450:])
            with self._lock:
                self._state, self._phase, self._message, self._progress = "ready", "ready", "登录浏览器已准备", 100
        except Exception as exc:
            with self._lock:
                self._state, self._phase, self._message = "failed", "failed", str(exc)[:600]
        finally:
            timer.cancel()
            with self._lock:
                self._proc = None
                self._speed_bps = None
                self._active = False
                self._done.set()

    def _event(self, event):
        with self._lock:
            if event["kind"] == "download":
                url = event["url"]
                if url not in self._routes.get(event["component"], []):
                    return
                self._source = _source(url, custom=bool(self._catalog_data.get("custom")) and event["component"] == "chromium")
                self._attempt, self._attempts = event["attempt"], event["attempts"]
                self._phase = "fallback" if self._attempt > 1 else "downloading"
                self._message = "正在下载 " + event["component"] + " · " + self._source["label"]
                self._downloaded_bytes = self._total_bytes = self._speed_bps = self._progress = None
            elif event["kind"] == "progress":
                self._downloaded_bytes, self._total_bytes = event["downloaded_bytes"], event["total_bytes"]
                self._speed_bps = event["speed_bps"]
                self._progress = min(99, int(self._downloaded_bytes / self._total_bytes * 100)) if self._total_bytes else None
            elif event["kind"] == "verifying":
                self._phase, self._message = "verifying", "正在校验浏览器组件…"

    def ensure_ready(self, cancel_event=None, on_progress=None):
        state = self.start(retry=False)
        while not state["ready"]:
            if cancel_event and cancel_event.is_set():
                raise ComponentError("登录已取消")
            if state["state"] != "installing":
                raise ComponentError(state["message"])
            if on_progress:
                on_progress(state["message"])
            self._done.wait(.5)
            state = self.status()

    def _stop_process(self, proc):
        try:
            if proc.poll() is not None:
                return
            proc.stdin.write("cancel\n")
            proc.stdin.flush()
            proc.wait(timeout=2)
        except Exception:
            if proc.poll() is None:
                try:
                    if os.name == "nt" and isinstance(getattr(proc, "pid", None), int):
                        # PID comes exclusively from our own Popen; never target process names.
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    else:
                        proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        with self._lock:
                            self._message = "正在停止下载进程；退出前不会启动新下载"

    def _cancel_download(self, cancel):
        with self._lock:
            if cancel is not self._cancel:
                return
            cancel.set()
            proc = self._proc
        if proc:
            self._stop_process(proc)

    def cancel(self):
        self._cancel_download(self._cancel)


_instances: dict[str, BrowserComponents] = {}
_instances_lock = threading.Lock()


def browser_components() -> BrowserComponents:
    key = str(config.data_dir().resolve())
    with _instances_lock:
        return _instances.setdefault(key, BrowserComponents())
