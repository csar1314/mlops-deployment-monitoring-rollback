import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / ".env"
with path.open("x", encoding="utf-8") as output:
    output.write(f"MLOPS_ADMIN_TOKEN={secrets.token_urlsafe(32)}\n")
    output.write(f"GRAFANA_ADMIN_PASSWORD={secrets.token_urlsafe(24)}\nMLOPS_ENABLE_DEMO=1\n")
print("Created .env with random credentials. Keep this file private; existing files are never overwritten.")
