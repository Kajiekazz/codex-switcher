"""Read-only model preflight. A catalog is not proof of account entitlement."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import sqlite3
from contextlib import closing

from . import config
from .appserver import AppServerSession
from .auth_state import read_auth_state, read_auth_state_bytes


def access_error(value):
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "model_not_found", "model_not_available", "model_access_denied", "unsupported_model",
        "do not have access to model", "does not have access to model", "don't have access to",
        "not supported with your current plan", "not supported when using codex with a chatgpt",
        "模型无权限", "无权使用", "模型不可用",
    ))


def thread_models(thread_ids):
    if not thread_ids or not config.state_db_path().exists():
        return {}
    try:
        with closing(sqlite3.connect(config.state_db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            return {row[0]: row[1] for row in db.execute(
                "SELECT id,model FROM threads WHERE id IN (" + ",".join("?" for _ in thread_ids) + ")", thread_ids)
                if isinstance(row[1], str) and row[1]}
    except sqlite3.Error:
        return {}


def read_catalog(auth_bytes):
    # Never change the live auth file or borrow another account's cached catalog.
    with tempfile.TemporaryDirectory(prefix="switcher-models-") as folder:
        root = Path(folder)
        (root / "auth.json").write_bytes(auth_bytes)
        env = dict(os.environ, CODEX_HOME=folder)
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY"):
            env.pop(key, None)
        with AppServerSession(cwd=root, env=env) as session:
            session.initialize(timeout=12)
            models, cursor = {}, None
            for _ in range(8):
                reply = session.request("model/list", {"includeHidden": True, "limit": 100, "cursor": cursor}, timeout=12)
                if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
                    raise ValueError("invalid_catalog")
                for row in reply["data"]:
                    if not isinstance(row, dict):
                        continue
                    name = row.get("model") or row.get("id")
                    if isinstance(name, str):
                        models[name] = {"allowed": row.get("allowed") if type(row.get("allowed")) is bool else None}
                cursor = reply.get("nextCursor")
                if not cursor:
                    return models
            raise ValueError("incomplete_catalog")


class ModelAccessGuard:
    def __init__(self, store, auth_path, *, catalog=read_catalog, path=None, clock=time.time):
        self.store, self.auth_path, self.catalog, self.clock = store, Path(auth_path), catalog, clock
        self.path = Path(path or config.data_dir() / "model-access.json")
        self.lock = threading.RLock()
        self.cache = {}
        try:
            self.observations = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.observations, dict):
                self.observations = {}
        except (OSError, ValueError):
            self.observations = {}

    def _auth(self, email):
        try:
            raw = self.auth_path.read_bytes()
            if (read_auth_state_bytes(raw).get("email") or "").casefold() == email.casefold():
                return raw
        except (OSError, ValueError, AttributeError):
            pass
        return self.store.get_auth_snapshot(email)

    def check(self, email, models, *, automatic=False, source_plan=None, probe=True):
        models = sorted(set(value for value in models if isinstance(value, str) and value))
        if not models:
            return {"ok": True, "state": "no_model_requirement"}
        try:
            raw = self._auth(email)
            if not raw:
                raise ValueError("no_auth")
            target_plan = read_auth_state_bytes(raw).get("plan")
            signature = hashlib.sha256(raw).hexdigest()
            with self.lock:
                observed = self.observations.get(signature, {})
                for model in models:
                    evidence = observed.get(model) or {}
                    if self.clock() - float(evidence.get("at") or 0) < 1800 and evidence.get("allowed") is False:
                        return {"ok": False, "state": "model_access_denied", "model": model,
                                "message": "该账号此前被服务端拒绝使用此模型，请更换账号或在 Codex 中选择可用模型"}
                if all((observed.get(model) or {}).get("allowed") is True and
                       self.clock() - float(observed[model].get("at") or 0) < 1800 for model in models):
                    return {"ok": True, "state": "confirmed_by_execution"}
                cached = self.cache.get(signature)
            if (not cached or self.clock() - cached[0] > 300) and probe:
                try:
                    catalog = self.catalog(raw)
                except Exception:
                    catalog = None
                with self.lock:
                    self.cache[signature] = (self.clock(), catalog)
            elif cached and self.clock() - cached[0] <= 300:
                catalog = cached[1]
            else:
                catalog = None
            if catalog is not None:
                for model in models:
                    if model not in catalog or catalog[model].get("allowed") is False:
                        return {"ok": False, "state": "model_not_listed", "model": model,
                                "message": "该账号的模型目录未提供此模型，已暂缓自动续跑；请在 Codex 中确认模型权限"}
            # Catalogs can be bundled fallbacks. Never use presence alone to
            # claim cross-plan access. Same-plan recovery keeps working.
            if automatic and (not source_plan or not target_plan or source_plan.casefold() != target_plan.casefold()):
                return {"ok": False, "state": "model_access_unknown", "models": models,
                        "message": "跨套餐模型权限尚未确认，暂缓自动切号；可手动切换后通过 Switcher 续跑验证一次"}
            return {"ok": True, "state": "catalog_only" if catalog is not None else "model_access_unknown",
                    "message": "模型权限仍以实际执行结果为准"}
        except Exception:
            return {"ok": not automatic, "state": "model_access_unknown", "message": "暂时无法核对模型权限"}

    def observe(self, email, model, allowed):
        if not email or not model:
            return
        try:
            raw = self._auth(email)
            if not raw:
                return
            self.observe_signature(hashlib.sha256(raw).hexdigest(), model, allowed)
        except (OSError, ValueError, AttributeError):
            pass

    def observe_signature(self, signature, model, allowed):
        try:
            with self.lock:
                self.observations.setdefault(signature, {})[model] = {"allowed": bool(allowed), "at": self.clock()}
                if len(self.observations) > 100:
                    self.observations.pop(next(iter(self.observations)))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.observations), encoding="utf-8")
                os.replace(tmp, self.path)
        except (OSError, ValueError, AttributeError):
            pass

    def current_check(self, models, *, automatic=False):
        state = read_auth_state(self.auth_path)
        result = self.check(state.get("email") or "", models, automatic=automatic, source_plan=state.get("plan"), probe=False)
        result["account_signature"] = state.get("file_sha256")
        return result
