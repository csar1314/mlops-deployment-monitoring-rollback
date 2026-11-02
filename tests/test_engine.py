import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import training
from src.config import Policy
from src.engine import DomainError, Engine


def traffic(engine, n=5, labels=True):
    results = []
    for sample in training.samples()["samples"][:n]:
        result = engine.predict(sample["features"])
        results.append(result)
        if labels and result["success"]:
            engine.feedback(result["request_id"], sample["label"])
    return results


def fast_policy(engine, **updates):
    engine.update_policy(
        Policy(
            min_requests=3,
            min_labels=3,
            consecutive_breaches=2,
            min_breach_seconds=1,
            cooldown_seconds=5,
            **updates,
        )
    )


def trip(engine, kind="error"):
    fast_policy(engine)
    engine.deploy("v2")
    engine.set_fault(kind, delay_ms=40)
    traffic(engine)
    engine.evaluate()
    engine.clock.advance(2)
    traffic(engine)
    return engine.evaluate()


def test_training_registry_and_immutable_versions(engine):
    models = engine.store.models()
    assert {m["version"] for m in models} == {"v1", "v2"}
    assert all(m["train_samples"] + m["test_samples"] == 150 for m in models)
    assert all(m["metrics"]["accuracy"] >= 0.85 and len(m["sha256"]) == 64 for m in models)
    with pytest.raises(DomainError, match="不可覆盖"):
        engine.train("v1", "logistic")
    assert engine.train("v3", "logistic")["version"] == "v3"


def test_deploy_does_not_promote_candidate(engine):
    engine.deploy("v2")
    assert engine.state["active_version"] == "v2"
    assert engine.state["stable_version"] == "v1"


def test_error_rate_triggers_exactly_one_verified_rollback(engine):
    result = trip(engine)
    assert result["status"] == "rollback_succeeded"
    assert engine.state["active_version"] == "v1"
    assert result["rollback"]["verified"]
    assert all(row["success"] for row in traffic(engine))
    engine.evaluate()
    assert len([e for e in engine.store.events() if e["kind"] == "rollback_succeeded"]) == 1


def test_same_requests_cannot_inflate_consecutive_breaches(engine):
    fast_policy(engine)
    engine.deploy("v2")
    engine.set_fault("error")
    traffic(engine)
    engine.evaluate()
    for _ in range(5):
        engine.clock.advance()
        engine.evaluate()
    assert engine.state["breach_count"] == 1
    assert engine.state["active_version"] == "v2"


def test_insufficient_samples_and_missing_metrics_do_not_rollback(engine):
    fast_policy(engine)
    engine.deploy("v2")
    engine.set_fault("error")
    traffic(engine, 2)
    assert engine.evaluate()["status"] == "insufficient_data"
    engine.clock.advance(60)
    assert engine.evaluate()["status"] == "no_data"
    assert engine.state["active_version"] == "v2"


def test_short_breach_does_not_rollback(engine):
    fast_policy(engine)
    engine.deploy("v2")
    engine.set_fault("error")
    traffic(engine)
    engine.evaluate()
    engine.set_fault("none")
    engine.clock.advance(31)
    traffic(engine)
    assert engine.evaluate()["status"] == "healthy"
    assert engine.state["breach_count"] == 0


def test_minimum_duration_prevents_early_rollback(engine):
    fast_policy(engine)
    engine.deploy("v2")
    engine.set_fault("error")
    traffic(engine)
    engine.evaluate()
    traffic(engine)
    assert engine.evaluate()["status"] == "suspected"
    assert engine.state["active_version"] == "v2"


def test_latency_rollback_with_real_sleep(engine):
    fast_policy(engine, p95_ms_threshold=10)
    engine.deploy("v2")
    engine.set_fault("latency", delay_ms=25)
    traffic(engine)
    engine.evaluate()
    engine.clock.advance(2)
    traffic(engine)
    assert engine.evaluate()["status"] == "rollback_succeeded"


def test_accuracy_requires_labels_then_triggers_rollback(engine):
    fast_policy(engine)
    engine.deploy("v2")
    engine.set_fault("accuracy")
    traffic(engine, labels=False)
    assert engine.evaluate()["status"] == "healthy"
    assert engine.window()["accuracy"] is None
    traffic(engine)
    assert engine.evaluate()["status"] == "suspected"
    engine.clock.advance(2)
    traffic(engine)
    assert engine.evaluate()["status"] == "rollback_succeeded"


def test_cooldown_blocks_redeploy_and_auto_flapping(engine):
    trip(engine)
    assert engine.evaluate()["status"] == "cooldown"
    with pytest.raises(DomainError, match="冷却期"):
        engine.deploy("v2")
    engine.clock.advance(6)
    engine.deploy("v2")


def test_corrupt_candidate_is_rejected_and_original_kept(engine):
    (engine.artifacts / "v2.joblib").write_bytes(b"corrupted")
    with pytest.raises(DomainError):
        engine.deploy("v2")
    assert engine.state["active_version"] == "v1"
    assert not engine.state["manual_required"]
    assert engine.predict([5.1, 3.5, 1.4, 0.2])["success"]


def test_failed_rollback_stops_automation_until_manual_recovery(engine):
    fast_policy(engine)
    engine.deploy("v2")
    path = engine.artifacts / "v1.joblib"
    original = path.read_bytes()
    path.write_bytes(b"invalid")
    engine.set_fault("error")
    traffic(engine)
    engine.evaluate()
    engine.clock.advance(2)
    traffic(engine)
    assert engine.evaluate()["status"] == "manual_required"
    assert engine.state["active_version"] == "v2"
    traffic(engine)
    assert engine.evaluate()["status"] == "manual_required"
    assert len([e for e in engine.store.events() if e["kind"] == "rollback_failed"]) == 1
    path.write_bytes(original)
    engine.rollback(engine.state["deployment_id"])
    assert not engine.state["manual_required"]


def test_post_switch_validation_failure_restores_original_pointer(engine, monkeypatch):
    engine.deploy("v2")
    original_probe = training.probe
    calls = 0

    def fail_second(bundle):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("post-switch failure")
        return original_probe(bundle)

    monkeypatch.setattr(training, "probe", fail_second)
    with pytest.raises(DomainError):
        engine.rollback(engine.state["deployment_id"])
    assert engine.state["active_version"] == "v2" and engine.state["manual_required"]
    assert engine.predict([5.1, 3.5, 1.4, 0.2])["version"] == "v2"


def test_concurrent_duplicate_rollbacks_execute_once(engine):
    engine.deploy("v2")
    deployment = engine.state["deployment_id"]

    def rollback():
        try:
            return engine.rollback(deployment)["verified"]
        except DomainError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: rollback(), range(2))) == [False, True]
    assert len([e for e in engine.store.events() if e["kind"] == "rollback_succeeded"]) == 1


def test_previous_deployment_metrics_cannot_contaminate_new_window(engine):
    engine.set_fault("error")
    traffic(engine)
    old = engine.state["deployment_id"]
    engine.deploy("v2")
    traffic(engine)
    assert engine.window()["errors"] == 0 and engine.window()["deployment_id"] != old


def test_labels_are_auditable_and_idempotent(engine):
    result = engine.predict([5.1, 3.5, 1.4, 0.2])
    engine.feedback(result["request_id"], 0)
    engine.feedback(result["request_id"], 0)
    with pytest.raises(DomainError):
        engine.feedback(result["request_id"], 1)


def test_promotion_requires_healthy_labeled_traffic(engine):
    engine.deploy("v2")
    with pytest.raises(DomainError):
        engine.promote("v2")
    traffic(engine, 25)
    engine.promote("v2")
    assert engine.state["stable_version"] == "v2"


def test_expiring_fault_does_not_persist_forever(engine):
    engine.set_fault("error", duration_seconds=2)
    assert not traffic(engine, 1)[0]["success"]
    engine.clock.advance(3)
    assert traffic(engine, 1)[0]["success"]
    assert engine.status()["fault"] is None


def test_single_process_guard(engine):
    with pytest.raises(RuntimeError, match="另一个服务实例"):
        Engine(engine.settings)


def test_restart_preserves_version_stable_target_and_requests(engine):
    engine.deploy("v2")
    traffic(engine)
    deployment = engine.state["deployment_id"]
    engine.close()
    restored = Engine(engine.settings, engine.clock)
    try:
        assert restored.state["active_version"] == "v2" and restored.state["stable_version"] == "v1"
        assert restored.state["deployment_id"] == deployment and restored.window()["count"] == 5
    finally:
        restored.close()


def test_stable_version_alert_does_not_attempt_self_rollback(engine):
    fast_policy(engine)
    engine.set_fault("error")
    traffic(engine)
    assert engine.evaluate()["status"] == "stable_version_alert"
    assert not [e for e in engine.store.events() if e["kind"] == "rollback_started"]


def test_demo_abort_restores_original_policy_and_serving_state(engine):
    fast_policy(engine)
    policy = engine.state["policy"].copy()
    experiment = engine.prepare_demo("error")
    engine.deploy("v2", experiment["id"])
    engine.set_fault("error", experiment_id=experiment["id"])
    engine.abort_demo(experiment["id"])
    assert engine.state["active_version"] == "v1" and engine.state["fault"] is None
    assert engine.state["policy"] == policy and engine.state["experiment_id"] is None


def test_abandoned_browser_demo_is_cleaned_up(engine):
    experiment = engine.prepare_demo("error")
    engine.deploy("v2", experiment["id"])
    engine.clock.advance(121)
    engine.evaluate()
    assert engine.store.experiment(experiment["id"])["status"] == "aborted"
    assert engine.state["active_version"] == "v1" and engine.state["experiment_id"] is None


def test_forged_experiment_cannot_change_deployment(engine):
    with pytest.raises(DomainError):
        engine.deploy("v2", "missing")
    assert engine.state["active_version"] == "v1"


def test_full_demo_report_uses_observed_rollback(engine):
    experiment = engine.prepare_demo("error")
    engine.deploy("v2", experiment["id"])
    engine.set_fault("error", experiment_id=experiment["id"])
    with pytest.raises(DomainError):
        engine.finish_demo(experiment["id"])
    for _ in range(3):
        traffic(engine, 15)
        engine.evaluate()
        engine.clock.advance(1.1)
    assert engine.state["active_version"] == "v1"
    traffic(engine, 6)
    report = engine.finish_demo(experiment["id"])
    assert report["status"] == "passed" and all(report["checks"].values())
    path = engine.settings.data_dir / "reports" / f"{experiment['id']}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "passed"


def test_emergency_recovery_closes_failed_rollback_path(engine):
    engine.state["manual_required"] = True
    engine.active_model = None
    engine.train("recovery-v3", "logistic")
    result = engine.recover("recovery-v3")
    assert result["verified"]
    assert engine.state["active_version"] == engine.state["stable_version"] == "recovery-v3"
    assert not engine.state["manual_required"]
    assert traffic(engine, 1)[0]["success"]
