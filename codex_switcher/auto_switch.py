"""Opt-in automatic switching when the current account is out of quota.

Safety model
------------
- Disabled unless ``CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1``.
- Switches after the usage endpoint reports exhaustion or an opted-in idle
  chat has a confirmed usage-limit error (or an opt-in prediction triggers).
- Candidate accounts must have an encrypted auth snapshot and must pass a
  read-only quota probe themselves. Unknown/invalid candidates are skipped.
- Percentages are compared only within the same plan and window shape.
  Priority/recency chooses between incomparable groups. Unknown/exhausted
  quotas and known-incompatible continuation models are skipped.
- Every switch goes through :class:`SwitchManager`, so the normal encrypted
  backup, atomic write, verification and rollback rules still apply.
- Per-account cooldown, hourly switch cap and consecutive-failure circuit
  breaker prevent oscillation and retry storms.
- Tokens are never stored in the status payload or the audit log.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config
from .trend import predict_quota
from .quota_state import comparison_group, remaining, usage_state
from .identity import profile_ref


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(
            timespec="seconds"
        )
    except Exception:
        return None


def _quota_summary(quota: dict | None) -> dict | None:
    if not isinstance(quota, dict):
        return None
    return {
        "ok": bool(quota.get("ok")),
        "error": quota.get("error"),
        "plan": quota.get("plan"),
        "limit_reached": quota.get("limit_reached"),
        "allowed": quota.get("allowed"),
        "availability": usage_state(quota),
        "primary_window": quota.get("primary_window"),
        "secondary_window": quota.get("secondary_window"),
        "reset_credits": quota.get("reset_credits"),
        "fetched_at": quota.get("fetched_at"),
    }


def _auto_resume_summary(outcome: dict | None) -> dict | None:
    """Keep switch diagnostics without persisting goals or tool output."""
    if not isinstance(outcome, dict):
        return None
    job = outcome.get("job")
    job = job if isinstance(job, dict) else {}
    thread_ids = outcome.get("thread_ids", job.get("thread_ids"))
    return {
        "ok": outcome.get("ok") is True,
        "skipped": outcome.get("skipped"),
        "job_id": job.get("id"),
        "thread_count": len(thread_ids) if isinstance(thread_ids, list) else 0,
        "cancelled_stale_job": bool(outcome.get("cancelled_stale_job")),
        "failed": outcome.get("ok") is False or bool(outcome.get("error")),
    }


class AutoSwitchMonitor:
    def __init__(
        self,
        store,
        switch_manager,
        quota_service,
        *,
        enabled: Callable[[], bool] | None = None,
        interval_seconds: int | None = None,
        cooldown_seconds: int | None = None,
        max_switches_per_hour: int | None = None,
        failure_threshold: int | None = None,
        circuit_open_seconds: int = 1800,
        no_candidate_retry_seconds: int = 300,
        predictive_enabled: Callable[[], bool] | None = None,
        predictive_threshold_seconds: int | None = None,
        log_path: Path | None = None,
        clock: Callable[[], float] | None = None,
        max_probes: int | None = None,
        probe_workers: int = 6,
        reset_enabled: Callable[[], bool] | None = None,
        reset_threshold: int | None = None,
        reset_min_wait_hours: int | None = None,
        reset_max_per_week: int | None = None,
        reset_before_switch: Callable[[], bool] | None = None,
        on_quota_recovered=None,
        quota_recovery_needed=None,
        model_guard=None,
        required_models=None,
    ):
        self.store = store
        self.switch_manager = switch_manager
        self.quota_service = quota_service
        self._enabled_fn = enabled or config.auto_switch_enabled
        self.interval_seconds = int(
            interval_seconds
            if interval_seconds is not None
            else config.auto_switch_interval_seconds()
        )
        self.cooldown_seconds = int(
            cooldown_seconds
            if cooldown_seconds is not None
            else config.auto_switch_cooldown_seconds()
        )
        self.max_switches_per_hour = int(
            max_switches_per_hour
            if max_switches_per_hour is not None
            else config.auto_switch_max_per_hour()
        )
        self.failure_threshold = int(
            failure_threshold
            if failure_threshold is not None
            else config.auto_switch_failure_threshold()
        )
        self.circuit_open_seconds = int(circuit_open_seconds)
        self.no_candidate_retry_seconds = int(no_candidate_retry_seconds)
        self._predictive_fn = predictive_enabled or config.predictive_switch_enabled
        self.predictive_threshold_seconds = int(
            predictive_threshold_seconds
            if predictive_threshold_seconds is not None
            else config.predictive_threshold_seconds()
        )
        self._clock = clock or time.time
        # 0 == probe the whole pool. Probes run in parallel and reuse the pool
        # quota cache, so the default is no artificial cap.
        self.max_probes = int(
            max_probes if max_probes is not None else config.auto_switch_max_probes()
        )
        self.probe_workers = max(1, int(probe_workers))
        self._log_path = Path(log_path) if log_path else (
            config.data_dir() / "auto-switch.log"
        )
        self.on_quota_recovered = on_quota_recovered
        self.quota_recovery_needed = quota_recovery_needed
        self.model_guard, self.required_models = model_guard, required_models
        self._recovery_path = self._log_path.with_suffix(".recovery.json")
        self._desktop_recovery = {}
        try:
            recovery = json.loads(self._recovery_path.read_text(encoding="utf-8"))
            self._waiting_email = self._restore_ref(str(recovery.get("profile_id") or recovery.get("email") or ""))
            pending = recovery.get("desktop_restart")
            if isinstance(pending, dict) and pending.get("target_email"):
                self._desktop_recovery = pending
            retry = recovery.get("retry") or {}
            self._recovery_retry = {
                "profile_id": self._restore_ref(str(retry.get("profile_id") or retry.get("email") or "")),
                "attempts": min(6, max(0, int(retry.get("attempts") or 0))),
                "after": float(retry.get("after") or 0),
            }
        except (OSError, ValueError, AttributeError, TypeError):
            self._waiting_email = ""
            self._recovery_retry = {}

        # Reset-credit automation. Every credit is single use, so all knobs are
        # resolved once per tick (settings.json can change without a restart)
        # and the feature is off unless explicitly enabled.
        self._reset_enabled_fn = reset_enabled or config.auto_reset_enabled
        self._reset_before_switch_fn = (
            reset_before_switch or config.auto_reset_before_switch
        )
        self.reset_threshold = int(
            reset_threshold
            if reset_threshold is not None
            else config.auto_reset_secondary_threshold()
        )
        self.reset_min_wait_hours = int(
            reset_min_wait_hours
            if reset_min_wait_hours is not None
            else config.auto_reset_min_wait_hours()
        )
        self.reset_max_per_week = int(
            reset_max_per_week
            if reset_max_per_week is not None
            else config.auto_reset_max_per_week()
        )

        self._lock = threading.RLock()
        self._recovery_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

        self._last_check_at: str | None = None
        self._last_result = "not_started"
        self._last_error: str | None = None
        self._last_quota: dict | None = None
        self._last_prediction: dict | None = None
        self._consecutive_failures = 0
        self._circuit_until = 0.0
        self._next_check_after = 0.0
        self._next_possible_at = 0.0
        self._switches: deque[float] = deque()
        self._cooldowns: dict[str, float] = {}
        self._last_switch: dict | None = None
        self._resets: deque[float] = deque()
        self._last_reset: dict | None = None
        self._reset_gate_reason: str | None = None
        self._events: deque[dict] = deque(maxlen=30)

    # ------------------------------------------------------------------ state
    def _resolve_ref(self, ref: str) -> str:
        if not ref:
            return ""
        resolver = getattr(self.store, "resolve_auth_profile", None)
        profile = resolver(ref) if callable(resolver) else None
        return profile_ref(profile) if isinstance(profile, dict) else ref

    def _restore_ref(self, ref: str) -> str:
        try:
            return self._resolve_ref(ref)
        except ValueError:
            # Legacy email-only quota waits cannot select a workspace, but
            # must not discard an independently identified desktop recovery.
            return ""

    def _identity_fields(self, fields: dict) -> dict:
        fields = dict(fields)
        resolver = getattr(self.store, "resolve_auth_profile", None)
        for key in ("email", "current_email", "previous_email", "target_email"):
            ref = fields.get(key)
            if not isinstance(ref, str) or not ref.startswith("profile_"):
                continue
            id_key = key.replace("email", "profile_id")
            fields.setdefault(id_key, ref)
            try:
                profile = resolver(ref) if callable(resolver) else None
            except ValueError:
                profile = None
            fields[key] = profile.get("email") if isinstance(profile, dict) else None
        return fields

    def _current_matches(self, ref: str) -> bool:
        current = self.switch_manager.current()
        if not current.get("logged_in") or current.get("identity_error"):
            return False
        try:
            return self._resolve_ref(ref).casefold() == self._resolve_ref(profile_ref(current)).casefold()
        except ValueError:
            return False

    def enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    def predictive_enabled(self) -> bool:
        try:
            return bool(self._predictive_fn())
        except Exception:
            return False

    def reset_enabled(self) -> bool:
        try:
            return bool(self._reset_enabled_fn())
        except Exception:
            return False

    def _reset_before_switch(self) -> bool:
        try:
            return bool(self._reset_before_switch_fn())
        except Exception:
            return False

    def _live_reset_gate(self, now: float) -> str | None:
        """Evaluate the reset gate against the last known quota.

        The tick only refreshes ``_reset_gate_reason`` when it actually reaches
        the reset check, so after a settings change (or during a long cooldown)
        the reported reason would stay stale — e.g. still "disabled" right after
        the user enables the feature. Evaluating here keeps the panel honest;
        it is pure arithmetic on data already in memory.
        """
        quota = self._last_quota
        if not isinstance(quota, dict) or not quota:
            return self._reset_gate_reason
        ok, reason = self._reset_gate(quota, now)
        return "ok" if ok else reason

    def status(self) -> dict:
        now = self._clock()
        live_gate = self._live_reset_gate(now)
        with self._lock:
            switches = [t for t in self._switches if now - t < 3600]
            cooldowns = {
                email: _iso(until)
                for email, until in self._cooldowns.items()
                if until > now
            }
            return {
                "enabled": self.enabled(),
                "running": self._running,
                "switching_enabled": self.switch_manager.enabled(),
                "interval_seconds": self.interval_seconds,
                "cooldown_seconds": self.cooldown_seconds,
                "max_switches_per_hour": self.max_switches_per_hour,
                "failure_threshold": self.failure_threshold,
                "predictive_enabled": self.predictive_enabled(),
                "predictive_threshold_seconds": self.predictive_threshold_seconds,
                "last_check_at": self._last_check_at,
                "last_result": self._last_result,
                "last_error": self._last_error,
                "last_quota": self._last_quota,
                "last_prediction": getattr(self, "_last_prediction", None),
                "consecutive_failures": self._consecutive_failures,
                "circuit_open": bool(self._circuit_until and now < self._circuit_until),
                "circuit_open_until": _iso(self._circuit_until)
                if self._circuit_until and now < self._circuit_until
                else None,
                "switches_last_hour": len(switches),
                "cooldowns": cooldowns,
                "last_switch": self._last_switch,
                "desktop_recovery": {
                    "waiting": bool(self._desktop_recovery),
                    "state": ("waiting_dispatch" if self._desktop_recovery.get("desktop_ready") else "waiting_desktop") if self._desktop_recovery else None,
                    "attempts": self._desktop_recovery.get("attempts", 0),
                    "retry_at": _iso(self._desktop_recovery.get("after")),
                },
                "quota_recovery": {
                    "waiting": bool(self._waiting_email),
                    "attempts": self._recovery_retry.get("attempts", 0),
                    "retry_at": _iso(self._recovery_retry.get("after")),
                },
                "auto_reset": {
                    "enabled": self.reset_enabled(),
                    "before_switch": self._reset_before_switch(),
                    "secondary_threshold": self.reset_threshold,
                    "min_wait_hours": self.reset_min_wait_hours,
                    "max_per_week": self.reset_max_per_week,
                    "used_last_7d": len([t for t in self._resets if now - t < 7 * 86400]),
                    "gate": live_gate,
                    "last": self._last_reset,
                },
                "recent_events": list(self._events)[-10:],
                "log_path": str(self._log_path),
            }

    def _set_result(self, action: str, error: str | None = None, **extra) -> dict:
        with self._lock:
            self._last_check_at = _iso(self._clock())
            self._last_result = action
            self._last_error = error
        result = {"action": action, "error": error}
        result.update(self._identity_fields(extra))
        return result

    def _event(self, event: str, **fields) -> None:
        entry = {"ts": _iso(self._clock()), "event": event}
        entry.update(self._identity_fields(fields))
        with self._lock:
            self._events.append(entry)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ----------------------------------------------------------------- thread
    def start(self) -> bool:
        if not self.enabled():
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                name="codex-switcher-auto-switch",
                daemon=True,
            )
            self._thread.start()
        return True

    def _loop(self) -> None:
        # Give the service and the first quota call a moment to settle.
        delay = min(10, max(1, self.interval_seconds))
        if self._stop.wait(delay):
            return
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 - keep the monitor alive
                self._set_result("internal_error", str(exc)[:200])
                self._event("internal_error", detail=str(exc)[:200])
            delay = min(10, max(1, self.interval_seconds)) if self._desktop_recovery else max(1, self.interval_seconds)
            if self._stop.wait(delay):
                break

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        with self._lock:
            self._running = False
            self._thread = None

    # ------------------------------------------------------------------- tick
    def _remember_waiting(self, email: str) -> None:
        if self._waiting_email == email:
            return
        self._waiting_email = email
        self._save_recovery()

    def _save_recovery(self) -> None:
        try:
            with self._lock:
                self._recovery_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._recovery_path.with_suffix(".tmp")
                waiting = self._identity_fields({"email": self._waiting_email})
                tmp.write_text(json.dumps({**waiting, "profile_id": self._waiting_email,
                                           "retry": self._recovery_retry, "desktop_restart": self._desktop_recovery}), encoding="utf-8")
                tmp.replace(self._recovery_path)
        except OSError:
            pass

    def _recover_current(self, email: str) -> dict | None:
        with self._recovery_lock:
            return self._recover_current_locked(email)

    def _recover_current_locked(self, email: str) -> dict | None:
        try:
            email = self._resolve_ref(email)
        except ValueError:
            return self._set_result("quota_recovery_pending", "workspace_required")
        if not self._current_matches(email):
            return self._set_result("account_changed")
        waiting = bool(self._waiting_email and self._waiting_email.lower() == email.lower())
        try:
            blocked = bool(self.quota_recovery_needed and self.quota_recovery_needed())
        except Exception:
            return self._set_result("quota_recovery_pending", "resume_evidence_unavailable")
        if not waiting and not blocked:
            if self._recovery_retry:
                self._recovery_retry = {}
                self._save_recovery()
            return None
        if self.on_quota_recovered is None:
            self._remember_waiting("")
            return None
        retry = self._recovery_retry
        now = self._clock()
        if retry.get("profile_id", "").lower() == email.lower() and now < retry.get("after", 0):
            return self._set_result("quota_recovery_pending", retry_at=_iso(retry["after"]))
        self._remember_waiting(email)
        if not self._current_matches(email):
            return self._set_result("account_changed")
        try:
            outcome = self.on_quota_recovered(self.switch_manager.current().get("email") or email)
        except Exception:
            # Keep the transition pending so a transient metadata failure does
            # not lose the only recovery notification.
            return self._set_result("quota_recovery_pending", "resume_callback_failed")
        if not isinstance(outcome, dict) or outcome.get("ok") is False or outcome.get("skipped") == "job_running":
            return self._set_result("quota_recovery_pending", auto_resume=_auto_resume_summary(outcome))
        if outcome.get("job"):
            attempts = min(6, int(retry.get("attempts") or 0) + 1) if retry.get("profile_id", "").lower() == email.lower() else 1
            self._recovery_retry = {
                "profile_id": email, "attempts": attempts,
                "after": now + min(900, max(30, self.interval_seconds) * 2 ** attempts),
            }
        else:
            self._recovery_retry = {}
        self._remember_waiting("")
        summary = _auto_resume_summary(outcome)
        self._event("quota_recovered", auto_resume=summary)
        return self._set_result("quota_recovered", auto_resume=summary)

    def on_manual_reset(self, email: str, result: dict, quota: dict | None) -> dict:
        """Record a completed redemption and recover only the live account."""
        current = self.switch_manager.current()
        current_email = self._resolve_ref(profile_ref(current))
        try:
            target = self._resolve_ref((email or current_email).strip())
        except ValueError:
            return {"ok": True, "skipped": "workspace_required"}
        self._event("reset_consumed", source="manual", email=target,
                    credit_id=result.get("credit_id"), windows_reset=result.get("windows_reset"))
        if not current.get("logged_in") or target.lower() != current_email.lower():
            return {"ok": True, "skipped": "not_current_account"}
        self._recovery_retry = {}
        self._remember_waiting(current_email)
        if usage_state(quota) != "available":
            return {"ok": True, "skipped": "waiting_quota"}
        recovered = self._recover_current(current_email)
        return (recovered or {}).get("auto_resume") or {"ok": True, "skipped": "waiting_quota"}

    def _recover_desktop(self, now: float) -> dict | None:
        with self._recovery_lock:
            pending = self._desktop_recovery
            if not pending:
                return None
            if now < float(pending.get("after") or 0):
                return self._set_result("desktop_recovery_pending")
            try:
                outcome = self.switch_manager.recover_desktop_restart(pending)
            except Exception as exc:
                outcome = {"ok": False, "error": str(exc)[:200]}
            if outcome.get("skipped") == "account_changed":
                self._desktop_recovery = {}
                self._save_recovery()
                self._event("desktop_recovery_cancelled", reason="account_changed")
                return None
            if outcome.get("ok"):
                self._desktop_recovery = {}
                self._save_recovery()
                summary = _auto_resume_summary(outcome.get("auto_resume"))
                if self._last_switch and (self._last_switch.get("target_profile_id") or self._last_switch.get("target_email", "")).lower() == (pending.get("target_profile_id") or pending["target_email"]).lower():
                    self._last_switch["auto_resume"] = summary
                self._event("desktop_recovered", desktop_restart=outcome.get("desktop_restart"), auto_resume=summary)
                return self._set_result("desktop_recovered", desktop_restart=outcome.get("desktop_restart"), auto_resume=summary)
            pending.update(attempts=int(pending.get("attempts") or 0) + 1, after=self._clock() + 10,
                           desktop_ready=bool((outcome.get("desktop_restart") or {}).get("ok")))
            self._save_recovery()
            self._event("desktop_recovery_pending", desktop_restart=outcome.get("desktop_restart"), error=outcome.get("error"))
            return self._set_result("desktop_recovery_pending", desktop_restart=outcome.get("desktop_restart"))

    def tick_once(self) -> dict:
        now = self._clock()

        if not self.enabled():
            return self._set_result("disabled")
        if not self.switch_manager.enabled():
            return self._set_result("switching_disabled")

        # Finish the account switch before consulting quota/network/cooldowns.
        recovered = self._recover_desktop(now)
        if recovered is not None:
            return recovered

        with self._lock:
            if self._circuit_until and now < self._circuit_until:
                return self._set_result(
                    "circuit_open",
                    circuit_open_until=_iso(self._circuit_until),
                )
            if self._circuit_until and now >= self._circuit_until:
                self._circuit_until = 0.0
                self._consecutive_failures = 0
            if now < self._next_check_after:
                return self._set_result("cooldown")

        current = self.switch_manager.current()
        if not current.get("logged_in"):
            return self._set_result("no_auth", current.get("reason"))

        if current.get("identity_error"):
            return self._set_result("identity_conflict", current.get("identity_error"))
        current_ref = self._resolve_ref(profile_ref(current))
        quota = self.quota_service.current()
        if not self._current_matches(current_ref):
            return self._set_result("account_changed")
        if quota.get("profile_id") and self._resolve_ref(quota["profile_id"]) != current_ref:
            return self._set_result("account_changed")
        self._last_quota = _quota_summary(quota)
        if not quota.get("ok"):
            error = quota.get("error") or "quota_error"
            self._event("quota_error", error=error)
            return self._set_result("quota_error", error)

        state = usage_state(quota)
        if state == "unknown":
            return self._set_result("quota_unknown", "额度接口尚未明确返回是否可用，等待下次检测")
        limit_reached = state == "exhausted"
        current_email = (current.get("email") or "").strip()
        self._waiting_email = self._restore_ref(self._waiting_email)
        if self._waiting_email and self._waiting_email.lower() != current_ref.lower():
            self._remember_waiting("")
        execution_limited = False
        if limit_reached:
            if self._recovery_retry:
                self._recovery_retry = {}
                self._save_recovery()
            self._remember_waiting(current_ref)
        else:
            # A turn can hit a model/account limit while the generic usage
            # endpoint still says available. Prefer a verified account switch
            # instead of silently returning quota_ok forever.
            try:
                execution_limited = bool(self.quota_recovery_needed and self.quota_recovery_needed())
            except Exception:
                return self._set_result("quota_recovery_pending", "resume_evidence_unavailable")
            recovered = self._recover_current(current_ref) if self._waiting_email else None
            if recovered is not None:
                return recovered
        trigger = "limit_reached" if limit_reached else "execution_limit_reached" if execution_limited else None
        prediction = None
        if trigger is None:
            if self.predictive_enabled():
                prediction = self._predict(current_ref)
                if prediction is not None:
                    with self._lock:
                        self._last_prediction = prediction
                    eta = prediction.get("soonest_eta_seconds")
                    if (
                        eta is not None
                        and eta <= self.predictive_threshold_seconds
                    ):
                        trigger = "predicted_exhaustion"
            if trigger is None:
                # Only a healthy quota clears stale switch failures. A quota
                # that is still exhausted must not reset the counter.
                with self._lock:
                    self._consecutive_failures = 0
                    if self._recovery_retry:
                        self._recovery_retry = {}
                        self._save_recovery()
                return self._set_result("quota_ok", prediction=prediction)

        with self._lock:
            self._prune_switches(now)
            if len(self._switches) >= self.max_switches_per_hour:
                return self._set_result(
                    "max_switches",
                    trigger=trigger,
                    switches_last_hour=len(self._switches),
                )

        current_email = (current.get("email") or "").strip()
        if self.reset_enabled() and self._reset_before_switch():
            early = self._try_auto_reset(quota, current_ref, now, trigger)
            if early is not None:
                return early

        candidate = self._choose_candidate(current_ref, now)
        if candidate is None:
            if execution_limited:
                recovered = self._recover_current(current_ref)
                if recovered is not None:
                    return recovered
            # Last resort: the pool is genuinely dry. Spending a reset credit
            # only makes sense when the weekly window is what blocks us.
            reset = self._try_auto_reset(quota, current_ref, now, trigger)
            if reset is not None:
                return reset
            retry_at = now + self.no_candidate_retry_seconds
            # Every candidate is exhausted: if the first window resets soon,
            # sleep until right after it instead of polling blind.
            hint = self._next_possible_at
            if hint and now < hint <= now + 900:
                retry_at = hint + 5
            with self._lock:
                self._next_check_after = retry_at
            self._event("no_candidate", current_email=current_email, trigger=trigger)
            return self._set_result(
                "no_candidate", trigger=trigger, reset_gate=self._reset_gate_reason
            )

        target_email = candidate.get("email") or ""
        target_ref = profile_ref(candidate)
        # Candidate probes can take time. A manual switch during that work
        # invalidates this decision and its captured recovery context.
        if not self._current_matches(current_ref):
            return self._set_result("account_changed")
        try:
            switch_result = self.switch_manager.switch_to(
                target_ref, confirm=True
            )
        except Exception as exc:  # noqa: BLE001 - includes SwitchError
            with self._lock:
                self._consecutive_failures += 1
                self._cooldowns[target_ref.lower()] = now + self.cooldown_seconds
                open_circuit = self._consecutive_failures >= self.failure_threshold
                if open_circuit:
                    self._circuit_until = now + self.circuit_open_seconds
            detail = getattr(exc, "message", None) or str(exc)
            self._event(
                "switch_failed",
                target_email=target_email,
                error=detail[:200],
                consecutive_failures=self._consecutive_failures,
            )
            if open_circuit:
                self._event(
                    "circuit_open",
                    until=_iso(self._circuit_until),
                    failures=self._consecutive_failures,
                )
            return self._set_result(
                "switch_failed",
                detail[:200],
                target_email=target_email,
                trigger=trigger,
                circuit_open=bool(open_circuit),
            )

        public = {}
        if hasattr(switch_result, "to_public"):
            try:
                public = switch_result.to_public()
            except Exception:
                public = {}
        auto_resume = _auto_resume_summary(public.get("auto_resume"))

        with self._lock:
            self._switches.append(now)
            self._prune_switches(now)
            self._consecutive_failures = 0
            # Do not switch back to the exhausted account before its reset.
            reset_at = None
            primary = quota.get("primary_window") or {}
            try:
                reset_at = float(primary.get("reset_at") or 0)
            except (TypeError, ValueError):
                reset_at = 0.0
            self._cooldowns[current_ref.lower()] = max(
                now + self.cooldown_seconds,
                reset_at if reset_at > now else 0.0,
            )
            self._cooldowns.pop(target_ref.lower(), None)
            self._last_switch = {
                "previous_email": public.get("previous_email") or current_email,
                "target_email": public.get("target_email") or target_email,
                "previous_profile_id": public.get("previous_profile_id") or current_ref,
                "target_profile_id": public.get("target_profile_id") or target_ref,
                "switched_at": public.get("switched_at") or _iso(now),
                "backup_path": public.get("backup_path"),
                "auto_resume": auto_resume,
            }
            self._next_check_after = now + min(30, max(5, self.interval_seconds))

        self.quota_service.invalidate()
        self._recovery_retry = {}
        self._remember_waiting("")
        desktop_restart = public.get("desktop_restart") or None
        if (isinstance(desktop_restart, dict) and desktop_restart.get("ok") is False
                and not desktop_restart.get("skipped") and desktop_restart.get("reason") in {
                    "restart_not_confirmed", "restart_status_unknown", "launch_failed",
                    "launch_stuck", "relaunch_not_detected",
                }):
            self._desktop_recovery = {
                "target_email": target_email,
                "target_profile_id": target_ref,
                "previous_pids": desktop_restart.get("previous_pids") or [],
                "resume_context": getattr(switch_result, "resume_context", None),
                "attempts": 0, "after": self._clock() + 10,
            }
        else:
            self._desktop_recovery = {}
        self._save_recovery()
        self._event(
            "switched",
            previous_email=current_email,
            previous_profile_id=current_ref,
            target_email=target_email,
            target_profile_id=target_ref,
            trigger=trigger,
            desktop_restart=desktop_restart,
            auto_resume=auto_resume,
        )
        # A disabled/unavailable desktop restart used to be completely silent:
        # the switch looked fine while the app kept showing the old account.
        # Log it as its own event so the failure is greppable.
        if isinstance(desktop_restart, dict) and desktop_restart.get("ok") is not True:
            self._event(
                "desktop_restart_failed",
                reason=desktop_restart.get("reason") or "unknown",
                message=desktop_restart.get("message"),
                previous_pids=desktop_restart.get("previous_pids"),
            )
        return self._set_result(
            "switched",
            previous_email=current_email,
            previous_profile_id=current_ref,
            target_email=target_email,
            target_profile_id=target_ref,
            trigger=trigger,
            desktop_restart=desktop_restart,
            auto_resume=auto_resume,
        )

    def _predict(self, email: str) -> dict | None:
        """Best-effort burn-rate prediction from the local sample history."""
        history = getattr(self.store, "quota_history", None)
        if history is None or not email:
            return None
        try:
            samples = history(email, hours=6)
            return predict_quota(self._last_quota, samples, now=self._clock())
        except Exception:  # noqa: BLE001 - prediction is advisory only
            return None

    def _prune_switches(self, now: float) -> None:
        while self._switches and now - self._switches[0] >= 3600:
            self._switches.popleft()

    @staticmethod
    def _remaining(quota: dict) -> tuple[float, float]:
        """Missing percentages rank below known ones, never as 100%."""
        return tuple(-1.0 if (value := remaining(quota.get(key))) is None else value
                     for key in ("primary_window", "secondary_window"))

    @staticmethod
    def _reset_at(quota: dict) -> float:
        """Epoch seconds of the earliest window reset, or 0 when unknown."""
        for key in ("primary_window", "secondary_window"):
            window = quota.get(key)
            if not isinstance(window, dict):
                continue
            try:
                value = float(window.get("reset_at") or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    def _probe_one(self, email: str) -> dict | None:
        """Quota probe for one account; ``None`` means the probe itself failed."""
        try:
            try:
                return self.quota_service.for_email(
                    email,
                    ttl=float(config.pool_quota_ttl_seconds()),
                    scope="pool",
                )
            except TypeError:
                # Quota doubles in tests/older builds take only the email.
                return self.quota_service.for_email(email)
        except Exception as exc:  # noqa: BLE001
            self._event(
                "candidate_probe_failed",
                email=email,
                error=str(exc)[:160],
            )
            return None

    def _probe_many(self, emails: list[str]) -> list[dict | None]:
        """Probe the whole pool in parallel (results keep ``emails`` order)."""
        if not emails:
            return []
        if len(emails) == 1:
            return [self._probe_one(emails[0])]
        workers = min(self.probe_workers, len(emails))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._probe_one, emails))

    def _reset_gate(self, quota: dict, now: float) -> tuple[bool, str]:
        """Decide whether spending an irreversible reset credit is justified.

        Order of the checks mirrors the cost model: the 5h window refills by
        itself every few hours, so a credit is only worth it when the **weekly**
        window is the binding constraint *and* it will not refill on its own
        within ``reset_min_wait_hours``.
        """
        if not self.reset_enabled():
            return False, "disabled"
        secondary = quota.get("secondary_window")
        if not isinstance(secondary, dict):
            return False, "no_secondary"
        try:
            used = float(secondary.get("used_percent") or 0)
        except (TypeError, ValueError):
            return False, "no_secondary"
        if used < self.reset_threshold:
            return False, "below_threshold"
        try:
            after = float(secondary.get("reset_after_seconds") or 0)
        except (TypeError, ValueError):
            after = 0.0
        floor = max(0, self.reset_min_wait_hours) * 3600
        if after and after < floor:
            return False, "window_resets_soon"
        credits = quota.get("reset_credits")
        if not isinstance(credits, dict) or not credits.get("known"):
            return False, "unknown"
        try:
            available = int(credits.get("applicable_available_count") or 0)
        except (TypeError, ValueError):
            available = 0
        if available < 1:
            try:
                owned = int(credits.get("available_count") or 0)
            except (TypeError, ValueError):
                owned = 0
            if owned > 0:
                return False, "not_applicable"
            return False, "no_credit"
        with self._lock:
            while self._resets and now - self._resets[0] >= 7 * 86400:
                self._resets.popleft()
            if len(self._resets) >= self.reset_max_per_week:
                return False, "weekly_cap"
        return True, "ok"

    def _try_auto_reset(
        self,
        quota: dict,
        current_email: str,
        now: float,
        trigger: str | None,
    ) -> dict | None:
        """Redeem one reset credit for this identity, never another workspace."""
        if not self._current_matches(current_email):
            return self._set_result("account_changed")
        ok, reason = self._reset_gate(quota, now)
        self._reset_gate_reason = reason
        if not ok:
            return None

        secondary = quota.get("secondary_window") or {}
        try:
            used = float(secondary.get("used_percent") or 0)
        except (TypeError, ValueError):
            used = 0.0
        redeem_request_id = str(uuid.uuid4())
        try:
            result = self.quota_service.consume_reset(
                email=current_email, redeem_request_id=redeem_request_id
            )
        except Exception as exc:  # noqa: BLE001 - never kill the monitor thread
            result = {
                "ok": False,
                "error": "request_failed",
                "message": str(exc)[:200],
            }

        if not result.get("ok"):
            error = result.get("error") or "reset_failed"
            self._event(
                "reset_failed",
                email=current_email,
                error=error,
                message=result.get("message"),
            )
            with self._lock:
                self._last_reset = self._identity_fields({
                    "email": current_email,
                    "ok": False,
                    "error": error,
                    "at": _iso(now),
                })
            # Fall through: the caller still has to report no_candidate.
            return None

        with self._lock:
            self._resets.append(now)
            self._cooldowns.pop((current_email or "").lower(), None)
            self._consecutive_failures = 0
            self._next_check_after = now + min(30, max(5, self.interval_seconds))
            self._last_reset = self._identity_fields({
                "email": current_email,
                "ok": True,
                "credit_id": result.get("credit_id"),
                "windows_reset": result.get("windows_reset"),
                "secondary_before": round(used, 1),
                "redeem_request_id": redeem_request_id,
                "at": _iso(now),
            })
        self.quota_service.invalidate()
        self._event(
            "reset_consumed",
            email=current_email,
            trigger=trigger,
            credit_id=result.get("credit_id"),
            windows_reset=result.get("windows_reset"),
            secondary_before=round(used, 1),
        )
        recovery = None
        if self.on_quota_recovered is not None:
            try:
                fresh = self.quota_service.current(force=True)
                if usage_state(fresh) == "available" and self._current_matches(current_email):
                    self._recovery_retry = {}
                    self._remember_waiting(current_email)
                    recovery = self._recover_current(current_email)
            except Exception:
                # The redemption is already complete; the normal monitor
                # retries recovery without redeeming another credit.
                pass
        return self._set_result(
            "reset_consumed",
            trigger=trigger,
            email=current_email,
            credit_id=result.get("credit_id"),
            windows_reset=result.get("windows_reset"),
            auto_resume=(recovery or {}).get("auto_resume"),
        )

    def _choose_candidate(self, current_email: str, now: float) -> dict | None:
        self._next_possible_at = 0.0
        models = self.required_models() if self.required_models else []
        source_plan = (self.switch_manager.current() or {}).get("plan")
        try:
            snapshots = self.store.list_auth_snapshots()
        except Exception:
            return None
        try:
            current_lower = self._resolve_ref(current_email).lower()
        except ValueError:
            return None
        candidates = []
        with self._lock:
            # An official refresh may fill in a formerly missing user_id.
            # Preserve cooldowns when storage promotes that profile's key.
            normalized = {}
            for ref, until in self._cooldowns.items():
                canonical = self._restore_ref(ref)
                if canonical:
                    normalized[canonical] = max(until, normalized.get(canonical, 0))
            self._cooldowns = normalized
            for snapshot in snapshots:
                email = profile_ref(snapshot)
                if not email or email.lower() == current_lower:
                    continue
                if snapshot.get("disabled"):
                    continue
                if self._cooldowns.get(email.lower(), 0.0) > now:
                    continue
                candidates.append(snapshot)

        # Probe order is *not* the pick order: it only decides who gets asked
        # first when ``max_probes`` caps the pool. Higher priority, then a
        # non-expired token, then the most recently saved.
        candidates.sort(
            key=lambda item: (
                int(item.get("priority") or 0),
                not bool(item.get("expired")),
                item.get("saved_at") or "",
            ),
            reverse=True,
        )

        probe_list = (
            candidates if self.max_probes <= 0 else candidates[: self.max_probes]
        )
        quotas = self._probe_many(
            [profile_ref(snapshot) for snapshot in probe_list]
        )

        usable: list[tuple[dict, tuple[float, float]]] = []
        next_reset = 0.0
        for snapshot, quota in zip(probe_list, quotas):
            if quota is None:
                continue
            email = profile_ref(snapshot)
            if quota.get("profile_id") and self._resolve_ref(quota["profile_id"]) != email:
                self._event("candidate_identity_mismatch", email=email)
                continue
            if not quota.get("ok"):
                if quota.get("error") in (
                    "auth_error",
                    "snapshot_not_found",
                    "no_chatgpt_token",
                ):
                    with self._lock:
                        self._cooldowns[email.lower()] = now + min(
                            self.cooldown_seconds, 900
                        )
                continue
            if usage_state(quota) == "unknown":
                self._event("candidate_unknown", email=email)
                continue
            if usage_state(quota) == "exhausted":
                # Probing an exhausted account again 60s later cannot change
                # the answer, so park it until its window resets.
                until = now + min(self.cooldown_seconds, 900)
                reset_at = self._reset_at(quota)
                if reset_at > now:
                    until = max(until, reset_at)
                    next_reset = (
                        reset_at if not next_reset else min(next_reset, reset_at)
                    )
                with self._lock:
                    self._cooldowns[email.lower()] = until
                continue
            if self.model_guard and models:
                access = self.model_guard.check(email, models, automatic=True, source_plan=source_plan)
                if not access.get("ok"):
                    self._event("candidate_model_blocked", email=email, reason=access.get("state"), message=access.get("message"))
                    continue
            usable.append((snapshot, self._remaining(quota), comparison_group(quota)))

        self._next_possible_at = next_reset
        if not usable:
            return None

        # Different plans/windows have different denominators. User priority,
        # then recency chooses a group; compare percentages only inside it.
        preferred = max(usable, key=lambda row: (int(row[0].get("priority") or 0), row[0].get("saved_at") or ""))[2]
        comparable = [row for row in usable if row[2] == preferred]
        comparable.sort(
            key=lambda pair: (
                pair[1][0],
                pair[1][1],
                int(pair[0].get("priority") or 0),
                pair[0].get("saved_at") or "",
            ),
            reverse=True,
        )
        chosen, headroom, _ = comparable[0]
        self._event(
            "candidate_chosen",
            email=chosen.get("email") or "",
            profile_id=profile_ref(chosen),
            primary_remaining=round(headroom[0], 1) if headroom[0] >= 0 else None,
            secondary_remaining=round(headroom[1], 1) if headroom[1] >= 0 else None,
            usable=len(usable),
        )
        return chosen
