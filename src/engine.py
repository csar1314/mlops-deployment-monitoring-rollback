import copy
import json
import logging
import re
import threading
import time
import uuid

import numpy as np
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from . import training
from .config import Policy
from .instance_lock import InstanceLock
from .store import Store

logger = logging.getLogger(__name__)


class DomainError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class Engine:
    def __init__(self, settings, clock=time.time):
        self.settings, self.clock = settings, clock
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.instance_lock = InstanceLock(settings.data_dir / "api.lock")
        self.lock = threading.RLock()
        self.training_lock = threading.Lock()
        self.artifacts = settings.data_dir / "artifacts"
        self.stop = threading.Event()
        self.thread = None
        self.active_model = None
        self.last_prune = clock()
        self.registry = CollectorRegistry()
        self.request_counter = Counter(
            "mlops_predictions_total", "Completed predictions", ["version", "result"], registry=self.registry
        )
        self.latency = Histogram(
            "mlops_prediction_duration_seconds",
            "Prediction duration",
            ["version"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.18, 0.25, 0.5, 1, 2, 5),
            registry=self.registry,
        )
        self.rollbacks = Counter(
            "mlops_rollbacks_total", "Rollback outcomes", ["result"], registry=self.registry
        )
        for result in ("success", "failure"):
            self.rollbacks.labels(result).inc(0)
        self.active_gauge = Gauge(
            "mlops_active_model", "Currently active model", ["version"], registry=self.registry
        )
        self.ready_gauge = Gauge("mlops_ready", "Model ready", registry=self.registry)
        self.controller_gauge = Gauge(
            "mlops_controller_last_evaluation_timestamp",
            "Last successful rule evaluation",
            registry=self.registry,
        )
        try:
            settings.ensure_token()
            self.store = Store(settings.data_dir / "mlops.sqlite3")
            self.state = self.store.state() or {
                "active_version": None,
                "stable_version": None,
                "deployment_id": None,
                "policy": Policy().model_dump(),
                "manual_required": False,
                "cooldown_until": 0,
                "fault": None,
                "experiment_id": None,
                "controller": {},
                "last_evaluated_seq": 0,
                "breach_count": 0,
                "breach_since": None,
            }
            if self.state["active_version"]:
                self._recover()
            elif settings.bootstrap:
                for version, algorithm in (("v1", "logistic"), ("v2", "forest")):
                    if not self.store.model(version):
                        self.train(version, algorithm)
                self._switch("v1", "bootstrap", "首次启动：发布已验证的基线模型", stable=True)
            if self.state.get("experiment_id"):
                self.abort_demo(self.state["experiment_id"], "服务重启，中断上一次演练")
            if settings.controller_enabled:
                self.thread = threading.Thread(
                    target=self._controller_loop, name="rollback-controller", daemon=True
                )
                self.thread.start()
        except Exception:
            self.instance_lock.close()
            raise

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=10)
        self.instance_lock.close()

    def _event(self, kind, payload=None, level="info"):
        with self.store.connect() as connection:
            self.store.event(connection, self.clock(), kind, payload, level)

    def _persist(self):
        with self.store.connect() as connection:
            self.store.save_state(connection, self.state)

    def _recover(self):
        try:
            metadata = self.store.model(self.state["active_version"])
            self.active_model = training.load_verified(metadata, self.artifacts)
            self._event("service_restarted", {"version": metadata["version"]})
        except Exception as error:
            self.state["manual_required"] = True
            self._event("startup_validation_failed", {"error": str(error)}, "error")
            target = self.state["stable_version"]
            if target and target != self.state["active_version"]:
                self._switch(target, "rollback", "启动时当前模型校验失败，恢复稳定版本")
            else:
                self._persist()
        if self.active_model:
            self.ready_gauge.set(1)
            self.active_gauge.labels(self.state["active_version"]).set(1)

    def _guard_demo(self, experiment_id=None):
        if self.state["experiment_id"] and self.state["experiment_id"] != experiment_id:
            raise DomainError("闭环演练正在运行，请先完成或取消演练")

    def train(self, version, algorithm):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}", version):
            raise DomainError("模型版本名称不合法", 422)
        if algorithm not in {"logistic", "forest"}:
            raise DomainError("不支持的训练算法", 422)
        if not self.training_lock.acquire(blocking=False):
            raise DomainError("已有训练任务正在运行")
        try:
            if self.store.model(version):
                raise DomainError("模型版本不可覆盖，请使用新版本名称")
            self._event("training_started", {"version": version, "algorithm": algorithm})
            metadata = training.train(version, algorithm, self.artifacts, self.clock)
            with self.store.connect() as connection:
                connection.execute("INSERT INTO models VALUES(?,?)", (version, json.dumps(metadata)))
                self.store.event(connection, self.clock(), "model_registered", metadata)
            return metadata
        except DomainError:
            raise
        except Exception as error:
            self._event("training_failed", {"version": version, "error": str(error)}, "error")
            raise DomainError("模型训练失败，请检查事件日志", 500) from error
        finally:
            self.training_lock.release()

    def _load(self, version):
        metadata = self.store.model(version)
        if not metadata:
            raise DomainError("模型版本不存在", 404)
        if metadata["metrics"]["accuracy"] < 0.85:
            raise DomainError("模型离线准确率低于发布门槛 0.85")
        return training.load_verified(metadata, self.artifacts)

    def _switch(self, target, action, reason, stable=False):
        """Hold the process lock throughout preflight, validation and atomic DB commit."""
        with self.lock:
            old = copy.deepcopy(self.state)
            previous_model = self.active_model
            operation_id, deployment_id = uuid.uuid4().hex, uuid.uuid4().hex
            started = time.perf_counter()
            payload = {
                "operation_id": operation_id,
                "from_version": old["active_version"],
                "to_version": target,
                "source_deployment_id": old["deployment_id"],
                "deployment_id": deployment_id,
                "reason": reason,
            }
            self._event(action + "_started", payload)
            try:
                model = self._load(target)
                self.active_model = model
                # Verify the actual serving pointer before committing the transition.
                training.probe(self.active_model)
                self.state.update(
                    {
                        "active_version": target,
                        "deployment_id": deployment_id,
                        "manual_required": False,
                        "fault": None,
                        "last_evaluated_seq": 0,
                        "breach_count": 0,
                        "breach_since": None,
                    }
                )
                if stable:
                    self.state["stable_version"] = target
                if action == "rollback":
                    self.state["cooldown_until"] = self.clock() + self.policy.cooldown_seconds
                payload.update(
                    {
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "verified": True,
                        "timestamp": self.clock(),
                        "action": action,
                    }
                )
                with self.store.connect() as connection:
                    connection.execute(
                        "INSERT INTO deployments VALUES(?,?,?)",
                        (deployment_id, self.clock(), json.dumps(payload)),
                    )
                    self.store.save_state(connection, self.state)
                    self.store.event(connection, self.clock(), action + "_succeeded", payload)
                for item in self.store.models():
                    self.active_gauge.labels(item["version"]).set(int(item["version"] == target))
                self.ready_gauge.set(1)
                if action == "rollback":
                    self.rollbacks.labels("success").inc()
                return payload
            except Exception as error:
                self.state, self.active_model = old, previous_model
                if action == "rollback":
                    self.state["manual_required"] = True
                    self.rollbacks.labels("failure").inc()
                with self.store.connect() as connection:
                    self.store.save_state(connection, self.state)
                    self.store.event(
                        connection,
                        self.clock(),
                        action + "_failed",
                        {**payload, "error": str(error)},
                        "error",
                    )
                raise DomainError(f"{action} 失败，原服务版本保留；详情见事件记录") from error

    @property
    def policy(self):
        return Policy(**self.state["policy"])

    def deploy(self, version, experiment_id=None):
        with self.lock:
            self._guard_demo(experiment_id)
            experiment = self._experiment(experiment_id) if experiment_id else None
            if self.state["manual_required"]:
                raise DomainError("系统需要人工处理，请先执行人工回滚或恢复")
            if self.clock() < self.state["cooldown_until"]:
                raise DomainError("系统处于回滚冷却期，请稍后发布")
            if version == self.state["active_version"]:
                raise DomainError("该版本已经在运行")
            result = self._switch(version, "deployment", "管理员发布候选版本")
            if experiment_id:
                experiment["candidate_deployment_id"] = result["deployment_id"]
                experiment["candidate_version"] = version
                with self.store.connect() as connection:
                    self.store.save_experiment(connection, experiment)
            return result

    def rollback(self, expected_deployment_id=None, automatic=False, reason="管理员人工回滚"):
        with self.lock:
            if expected_deployment_id and expected_deployment_id != self.state["deployment_id"]:
                raise DomainError("部署已变化，拒绝重复或过期的回滚请求")
            if automatic and self.state["manual_required"]:
                raise DomainError("自动回滚已停止，需要人工处理")
            target = self.state["stable_version"]
            if not target or target == self.state["active_version"]:
                raise DomainError("没有不同于当前版本的稳定回滚目标")
            return self._switch(target, "rollback", ("自动回滚：" if automatic else "人工回滚：") + reason)

    def recover(self, version):
        with self.lock:
            self._guard_demo()
            if not self.state["manual_required"] and self.active_model is not None:
                raise DomainError("只有需要人工处理或服务无可用模型时才可执行紧急恢复")
            return self._switch(version, "manual_recovery", "管理员明确指定已验证的紧急恢复版本", stable=True)

    def predict(self, features):
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        with self.lock:
            if self.active_model is None:
                raise DomainError("没有可用模型，需要人工恢复", 503)
            model = self.active_model
            version, deployment_id = self.state["active_version"], self.state["deployment_id"]
            fault = copy.deepcopy(self.state["fault"])
        if fault and (fault["version"] != version or fault["expires_at"] <= self.clock()):
            fault = None
        success, prediction, probabilities, error = False, None, None, None
        try:
            if fault and fault["kind"] == "error":
                raise RuntimeError("受控演练：注入预测服务错误")
            if fault and fault["kind"] == "latency":
                time.sleep(fault["delay_ms"] / 1000)
            with self.lock:
                # sklearn estimators here are immutable; serialize calls for deterministic demo behavior.
                prediction = int(model["model"].predict([features])[0])
                probabilities = model["model"].predict_proba([features])[0].tolist()
            if fault and fault["kind"] == "accuracy":
                prediction = (prediction + 1) % 3
                probabilities = np.roll(probabilities, 1).tolist()
            success = True
        except Exception as exception:
            error = str(exception)
            logger.warning("prediction failed version=%s request=%s error=%s", version, request_id, error)
        latency_ms = (time.perf_counter() - started) * 1000
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO requests(id,timestamp,deployment_id,version,success,latency_ms,prediction) VALUES(?,?,?,?,?,?,?)",
                (request_id, self.clock(), deployment_id, version, int(success), latency_ms, prediction),
            )
        self.request_counter.labels(version, "success" if success else "error").inc()
        self.latency.labels(version).observe(latency_ms / 1000)
        return {
            "request_id": request_id,
            "version": version,
            "deployment_id": deployment_id,
            "prediction": prediction,
            "probabilities": probabilities,
            "latency_ms": round(latency_ms, 3),
            "success": success,
            "error": error,
        }

    def feedback(self, request_id, label):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT success,label FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if not row:
                raise DomainError("请求记录不存在", 404)
            if not row["success"]:
                raise DomainError("失败的预测不能提交效果标签")
            if row["label"] is not None and row["label"] != label:
                raise DomainError("该请求已有不同标签，不能覆盖审计数据")
            connection.execute("UPDATE requests SET label=? WHERE id=?", (label, request_id))
        return {"accepted": True, "request_id": request_id}

    def window(self):
        with self.lock:
            rows = self.store.rows(
                "SELECT * FROM requests WHERE deployment_id=? AND timestamp>=? ORDER BY seq",
                (self.state["deployment_id"], self.clock() - self.policy.window_seconds),
            )
            labeled = [row for row in rows if row["success"] and row["label"] is not None]
            return {
                "deployment_id": self.state["deployment_id"],
                "version": self.state["active_version"],
                "count": len(rows),
                "errors": sum(not row["success"] for row in rows),
                "error_rate": sum(not row["success"] for row in rows) / len(rows) if rows else None,
                "p95_ms": float(np.percentile([row["latency_ms"] for row in rows], 95)) if rows else None,
                "labeled_count": len(labeled),
                "accuracy": sum(row["prediction"] == row["label"] for row in labeled) / len(labeled)
                if labeled
                else None,
                "last_seq": max((row["seq"] for row in rows), default=0),
            }

    def _breaches(self, window):
        policy = self.policy
        reasons = []
        if window["count"] >= policy.min_requests:
            if window["error_rate"] >= policy.error_rate_threshold:
                reasons.append("错误率超过阈值")
            if window["p95_ms"] > policy.p95_ms_threshold:
                reasons.append("P95 延迟超过阈值")
            if window["labeled_count"] >= policy.min_labels and window["accuracy"] < policy.accuracy_floor:
                reasons.append("带标签预测准确率低于阈值")
        return reasons

    def evaluate(self):
        with self.lock:
            if self.state["experiment_id"]:
                experiment = self.store.experiment(self.state["experiment_id"])
                if self.clock() - experiment["started_at"] > 120:
                    self.abort_demo(experiment["id"], "演练超过 120 秒未完成，自动清理")
            now, window = self.clock(), self.window()
            controller = {"evaluated_at": now, "window": window, "reasons": [], "status": "healthy"}
            if self.state["manual_required"]:
                controller["status"] = "manual_required"
            elif now < self.state["cooldown_until"]:
                controller["status"] = "cooldown"
            elif window["count"] < self.policy.min_requests:
                controller["status"] = "no_data" if window["count"] == 0 else "insufficient_data"
                self.state.update(breach_count=0, breach_since=None)
            else:
                reasons = self._breaches(window)
                controller["reasons"] = reasons
                if not reasons:
                    self.state.update(breach_count=0, breach_since=None)
                elif self.state["active_version"] == self.state["stable_version"]:
                    controller["status"] = "stable_version_alert"
                    if self.state["controller"].get("status") != "stable_version_alert":
                        self._event("stable_version_alert", {"reasons": reasons, "window": window}, "warning")
                else:
                    controller["status"] = "suspected"
                    if window["last_seq"] > self.state["last_evaluated_seq"]:
                        self.state["breach_count"] += 1
                        if self.state["breach_since"] is None:
                            self.state["breach_since"] = now
                    if (
                        self.state["breach_count"] >= self.policy.consecutive_breaches
                        and now - self.state["breach_since"] >= self.policy.min_breach_seconds
                    ):
                        controller["status"] = "rollback_triggered"
                        self._event(
                            "alert_triggered",
                            {
                                "window": window,
                                "reasons": reasons,
                                "breach_count": self.state["breach_count"],
                            },
                            "warning",
                        )
                        try:
                            result = self.rollback(
                                window["deployment_id"], automatic=True, reason="；".join(reasons)
                            )
                            controller["rollback"] = result
                            controller["status"] = "rollback_succeeded"
                        except DomainError as error:
                            controller.update(status="manual_required", error=str(error))
            # Sequence numbers are per deployment; after a switch restart at zero.
            self.state["last_evaluated_seq"] = (
                window["last_seq"] if window["deployment_id"] == self.state["deployment_id"] else 0
            )
            self.state["controller"] = controller
            self._persist()
            self.controller_gauge.set(now)
            if now - self.last_prune > 300:
                with self.store.connect() as connection:
                    connection.execute("DELETE FROM requests WHERE timestamp<?", (now - 86400,))
                self.last_prune = now
            return controller

    def _controller_loop(self):
        while not self.stop.wait(self.settings.interval):
            try:
                self.evaluate()
            except Exception:
                logger.exception("Controller evaluation failed")
                # A failing observer must never claim a successful evaluation.

    def status(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            if state["fault"] and state["fault"]["expires_at"] <= self.clock():
                state["fault"] = None
            heartbeat = state["controller"].get("evaluated_at")
            state.update(
                {
                    "ready": self.active_model is not None,
                    "window": self.window(),
                    "demo_enabled": self.settings.demo_enabled,
                    "controller_stale": heartbeat is None
                    or self.clock() - heartbeat > max(5, 5 * self.settings.interval),
                    "server_time": self.clock(),
                }
            )
            return state

    def update_policy(self, policy):
        with self.lock:
            self._guard_demo()
            self.state.update(policy=policy.model_dump(), breach_count=0, breach_since=None)
            with self.store.connect() as connection:
                self.store.save_state(connection, self.state)
                self.store.event(connection, self.clock(), "policy_updated", policy.model_dump())
            return self.state["policy"]

    def promote(self, version):
        with self.lock:
            self._guard_demo()
            window = self.window()
            if (
                version != self.state["active_version"]
                or window["count"] < max(self.policy.min_requests, self.policy.promotion_min_requests)
                or window["labeled_count"] < self.policy.min_labels
                or self._breaches(window)
                or self.state["manual_required"]
                or self.status()["fault"]
            ):
                raise DomainError("晋升需要当前版本、足够的健康请求与真实标签，且无故障或人工处理状态")
            self._load(version)
            self.state["stable_version"] = version
            with self.store.connect() as connection:
                self.store.save_state(connection, self.state)
                self.store.event(
                    connection, self.clock(), "model_promoted", {"version": version, "window": window}
                )
            return {"stable_version": version}

    def set_fault(self, kind, duration_seconds=60, delay_ms=350, experiment_id=None):
        with self.lock:
            if not self.settings.demo_enabled:
                raise DomainError("受控故障注入仅在演示模式开放", 403)
            self._guard_demo(experiment_id)
            experiment = self._experiment(experiment_id) if experiment_id else None
            if kind == "none":
                self.state["fault"] = None
            else:
                self.state["fault"] = {
                    "kind": kind,
                    "version": self.state["active_version"],
                    "expires_at": self.clock() + duration_seconds,
                    "delay_ms": delay_ms,
                }
                if experiment_id:
                    experiment.update(fault_at=self.clock(), fault_kind=kind)
                    with self.store.connect() as connection:
                        self.store.save_experiment(connection, experiment)
            with self.store.connect() as connection:
                self.store.save_state(connection, self.state)
                self.store.event(
                    connection,
                    self.clock(),
                    "fault_configured",
                    {"fault": self.state["fault"], "experiment_id": experiment_id},
                )
            return {"fault": self.state["fault"]}

    def _experiment(self, experiment_id):
        experiment = self.store.experiment(experiment_id)
        if not experiment:
            raise DomainError("演练记录不存在", 404)
        if self.state["experiment_id"] != experiment_id or experiment["status"] != "running":
            raise DomainError("演练已结束或不属于当前会话")
        return experiment

    def prepare_demo(self, kind):
        with self.lock:
            if not self.settings.demo_enabled:
                raise DomainError("请使用 --demo 启动以启用闭环演练", 403)
            self._guard_demo()
            for version in ("v1", "v2"):
                self._load(version)
            experiment_id = uuid.uuid4().hex
            experiment = {
                "id": experiment_id,
                "status": "running",
                "started_at": self.clock(),
                "fault_kind": kind,
                "original_policy": copy.deepcopy(self.state["policy"]),
            }
            self._switch("v1", "demo_baseline", "管理员启动隔离演练：恢复演示基线", stable=True)
            self.state.update(experiment_id=experiment_id, cooldown_until=0, policy=Policy().model_dump())
            with self.store.connect() as connection:
                self.store.save_experiment(connection, experiment)
                self.store.save_state(connection, self.state)
                self.store.event(
                    connection,
                    self.clock(),
                    "demo_started",
                    {"experiment_id": experiment_id, "kind": kind, "temporary_policy": self.state["policy"]},
                )
            return experiment

    def finish_demo(self, experiment_id):
        with self.lock:
            experiment = self._experiment(experiment_id)
            candidate = experiment.get("candidate_deployment_id")
            events = [
                row
                for row in reversed(self.store.events(1000))
                if row["timestamp"] >= experiment["started_at"]
            ]
            rollbacks = [
                row
                for row in events
                if row["kind"] == "rollback_succeeded"
                and row["payload"].get("source_deployment_id") == candidate
                and row["payload"]["reason"].startswith("自动回滚：")
            ]
            window = self.window()
            if (
                not candidate
                or not experiment.get("fault_at")
                or len(rollbacks) != 1
                or self.state["active_version"] != "v1"
                or window["count"] < 5
                or window["errors"]
                or window["labeled_count"] < 5
                or window["accuracy"] < 0.8
            ):
                raise DomainError("闭环未通过：需要一次自动回滚、至少 5 次带标签的恢复预测且准确率不低于 0.8")
            before = self.store.rows(
                "SELECT success,latency_ms,prediction,label FROM requests WHERE deployment_id=?", (candidate,)
            )
            labeled = [row for row in before if row["label"] is not None]
            rollback = rollbacks[0]
            experiment.update(
                {
                    "status": "passed",
                    "finished_at": self.clock(),
                    "recovery_seconds": round(rollback["timestamp"] - experiment["fault_at"], 3),
                    "rollback_duration_ms": rollback["payload"]["duration_ms"],
                    "candidate_requests": len(before),
                    "candidate_errors": sum(not row["success"] for row in before),
                    "candidate_accuracy": sum(row["prediction"] == row["label"] for row in labeled)
                    / len(labeled)
                    if labeled
                    else None,
                    "candidate_p95_ms": float(np.percentile([row["latency_ms"] for row in before], 95)),
                    "recovered_version": self.state["active_version"],
                    "recovery_window": window,
                    "event_sequence": events,
                    "checks": {
                        "auto_rollback_once": True,
                        "stable_version_restored": True,
                        "serving_verified": True,
                        "recovery_requests_passed": True,
                        "recovery_labeled_accuracy": True,
                    },
                }
            )
            self.state.update(experiment_id=None, policy=experiment["original_policy"])
            with self.store.connect() as connection:
                self.store.save_experiment(connection, experiment)
                self.store.save_state(connection, self.state)
                self.store.event(connection, self.clock(), "demo_passed", {"experiment_id": experiment_id})
            directory = self.settings.data_dir / "reports"
            directory.mkdir(exist_ok=True)
            (directory / f"{experiment_id}.json").write_text(
                json.dumps(experiment, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return experiment

    def abort_demo(self, experiment_id, reason="管理员取消演练"):
        with self.lock:
            experiment = self._experiment(experiment_id)
            self.state["fault"] = None
            experiment.update(status="aborted", finished_at=self.clock(), reason=reason)
            try:
                if self.state["active_version"] != "v1":
                    self._switch("v1", "demo_cleanup", reason, stable=True)
            except DomainError:
                self.state["manual_required"] = True
                raise
            finally:
                self.state.update(experiment_id=None, policy=experiment["original_policy"])
                with self.store.connect() as connection:
                    self.store.save_experiment(connection, experiment)
                    self.store.save_state(connection, self.state)
                    self.store.event(
                        connection,
                        self.clock(),
                        "demo_aborted",
                        {"experiment_id": experiment_id, "reason": reason},
                    )
            return experiment
