import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import Settings


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(tmp_path, token="test-secret", controller_enabled=False))) as client:
        yield client


def test_dashboard_docs_and_readiness(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/health/ready").json()["version"] == "v1"


def test_authentication_prevents_mutations(client):
    assert client.post("/api/deploy", json={"version": "v2"}).status_code == 401
    assert client.post("/api/control/evaluate").status_code == 401
    assert "test-secret" not in client.get("/api/status").text


@pytest.mark.parametrize("features", [[1, 2], [-1, 2, 3, 4], [1, 2, 3, 99], ["abc", 2, 3, 4]])
def test_invalid_input_is_rejected_without_polluting_predict_metrics(client, features):
    assert client.post("/api/predict", json={"features": features}).status_code == 422
    assert client.get("/api/status").json()["window"]["count"] == 0


def test_health_checks_do_not_count_as_predictions(client):
    for _ in range(3):
        client.get("/health/live")
        client.get("/health/ready")
    assert client.get("/api/status").json()["window"]["count"] == 0


def test_inference_feedback_and_prometheus_export(client):
    response = client.post("/api/predict", json={"features": [5.1, 3.5, 1.4, 0.2]})
    assert response.status_code == 200
    result = response.json()
    assert result["version"] == "v1" and result["prediction"] == 0
    assert (
        client.post(
            "/api/feedback",
            json={"request_id": result["request_id"], "label": 0},
            headers={"X-Admin-Token": "test-secret"},
        ).status_code
        == 200
    )
    metrics = client.get("/metrics")
    assert 'mlops_predictions_total{result="success",version="v1"} 1.0' in metrics.text
    assert "mlops_window_labeled_accuracy NaN" in metrics.text
    assert "mlops_policy_min_requests 12.0" in metrics.text
    assert "mlops_policy_p95_seconds_threshold 0.18" in metrics.text


def test_fault_injection_disabled_by_default(client):
    assert (
        client.post(
            "/api/fault", json={"kind": "error"}, headers={"X-Admin-Token": "test-secret"}
        ).status_code
        == 403
    )
    assert (
        client.post("/api/demo/prepare", json={}, headers={"X-Admin-Token": "test-secret"}).status_code == 403
    )


def test_invalid_policy_is_rejected(client):
    response = client.put(
        "/api/policy", json={"error_rate_threshold": 0}, headers={"X-Admin-Token": "test-secret"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999"])
def test_non_finite_inputs_return_valid_json_errors(client, value):
    response = client.post(
        "/api/predict",
        content=f'{{"features":[{value},3,1,0.2]}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422 and response.json()["detail"]
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/status").json()["window"]["count"] == 0
