"""Real HTTP end-to-end demonstrations; the independent controller must be running."""

import argparse
import json
import time
from pathlib import Path

import httpx


def run(client, kind, output):
    started = time.perf_counter()
    experiment_id = None
    sent = 0

    def post(path, body=None):
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    samples = client.get("/api/samples").json()["samples"]

    def prediction():
        nonlocal sent
        sample = samples[sent % len(samples)]
        sent += 1
        response = client.post("/api/predict", json={"features": sample["features"]})
        if response.status_code not in (200, 503):
            response.raise_for_status()
        result = response.json()
        if "request_id" not in result:
            raise RuntimeError(f"Prediction service unavailable: {result}")
        if result["success"]:
            post("/api/feedback", {"request_id": result["request_id"], "label": sample["label"]})
        return result

    try:
        experiment = post("/api/demo/prepare", {"kind": kind})
        experiment_id = experiment["id"]
        for _ in range(15):
            assert prediction()["success"], "Baseline prediction failed"
            time.sleep(0.05)
        post("/api/deploy", {"version": "v2", "experiment_id": experiment_id})
        for _ in range(10):
            assert prediction()["success"], "Candidate preflight prediction failed"
            time.sleep(0.05)
        post("/api/fault", {"kind": kind, "experiment_id": experiment_id})
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            prediction()
            status = client.get("/api/status").json()
            if status["manual_required"]:
                raise RuntimeError("Controller entered manual recovery state")
            if status["active_version"] == "v1":
                break
            time.sleep(0.12)
        else:
            raise TimeoutError("Independent controller did not roll back within 75 seconds")
        for _ in range(10):
            result = prediction()
            assert result["success"] and result["version"] == "v1", "Recovery verification failed"
        report = post(f"/api/demo/{experiment_id}/finish")
        experiment_id = None
        report["client_validation"] = {
            "transport": "HTTP",
            "predictions_sent": sent,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "controller_evaluation_called_by_client": False,
        }
        output.mkdir(parents=True, exist_ok=True)
        (output / f"demo-{kind}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"PASS {kind}: recovery={report['recovery_seconds']}s rollback={report['rollback_duration_ms']}ms requests={sent}",
            flush=True,
        )
        return report
    finally:
        if experiment_id:
            post(f"/api/demo/{experiment_id}/abort")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", type=Path, default=Path("runtime/admin-token.txt"))
    parser.add_argument("--kind", choices=["all", "error", "latency", "accuracy"], default="all")
    parser.add_argument("--output", type=Path, default=Path("reports/validation"))
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    with httpx.Client(
        base_url=args.base_url, headers={"X-Admin-Token": token}, timeout=20, trust_env=False
    ) as client:
        client.get("/health/ready").raise_for_status()
        for kind in ["error", "latency", "accuracy"] if args.kind == "all" else [args.kind]:
            run(client, kind, args.output)


if __name__ == "__main__":
    main()
