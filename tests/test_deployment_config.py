import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_components_and_mounts():
    config = yaml.safe_load((ROOT / "deploy/compose.yml").read_text())
    assert set(config["services"]) == {"api", "controller", "prometheus", "grafana"}
    assert config["services"]["api"]["environment"]["MLOPS_CONTROLLER"] == "0"
    assert config["services"]["controller"]["command"] == ["python", "-m", "src.controller"]
    for service in config["services"].values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:")
        for volume in service.get("volumes", []):
            source = volume.split(":")[0]
            if source.startswith(".."):
                assert (ROOT / "deploy" / source).exists()


def test_grafana_dashboard_and_prometheus_rules():
    dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/mlops.json").read_text(encoding="utf-8"))
    assert len(dashboard["panels"]) >= 6
    rules = yaml.safe_load((ROOT / "monitoring/alerts.yml").read_text())
    assert len(rules["groups"][0]["rules"]) == 5
