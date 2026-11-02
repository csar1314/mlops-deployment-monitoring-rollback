"""Optional separate controller process, used by Docker Compose."""

import logging
import os
import time

import httpx


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ["MLOPS_ADMIN_TOKEN"]
    base = os.getenv("MLOPS_API_URL", "http://127.0.0.1:8765")
    interval = float(os.getenv("MLOPS_CONTROLLER_INTERVAL", "1"))
    with httpx.Client(base_url=base, headers={"X-Admin-Token": token}, timeout=15, trust_env=False) as client:
        while True:
            try:
                response = client.post("/api/control/evaluate")
                response.raise_for_status()
            except httpx.HTTPError as error:
                logging.error("Controller cannot evaluate: %s", error)
            time.sleep(interval)


if __name__ == "__main__":
    main()
