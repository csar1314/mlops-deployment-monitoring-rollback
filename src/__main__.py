import argparse
import logging
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from .app import create_app
from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="MLOps 部署、监控与自动回滚系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--demo", action="store_true", help="启用受控故障演练")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.demo:
        settings.demo_enabled = True
    token = settings.ensure_token()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dashboard: {url}\nAdmin token file: {settings.data_dir / 'admin-token.txt'}", flush=True)
    if args.open_browser:

        def open_when_ready():
            for _ in range(90):
                try:
                    with urllib.request.urlopen(url + "/health/ready", timeout=1) as response:
                        if response.status == 200:
                            # URL fragment is never sent to the server or its access log.
                            webbrowser.open(url + "/#token=" + token)
                            return
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(1)

        threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1, access_log=True)


if __name__ == "__main__":
    main()
