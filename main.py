import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# PyInstaller 以 --noconsole 打包（windowed）时 sys.stdout 为 None，
# 后续 print / 日志打印会抛 AttributeError，这里先替换成空流，保证无控制台也能正常跑
if getattr(sys, "frozen", False) and sys.stdout is None:
    _null = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _null
    sys.stderr = _null

from app.engine import CrawlEngine
from app.repository import ArchiveRepository
from app.web import create_app


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_server(base, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/config/status", timeout=2):
                return True
        except Exception:
            time.sleep(0.3)
    return False


class _NoAccessLog(logging.Filter):
    # 请求访问日志形如 "... "GET /api/.. HTTP/1.1" 200"，含 " HTTP/"
    def filter(self, record):
        return " HTTP/" not in record.getMessage()


def main():
    lg = logging.getLogger("werkzeug")
    lg.setLevel(logging.INFO)
    lg.addFilter(_NoAccessLog())
    repository = ArchiveRepository(BASE_DIR)
    engine = CrawlEngine(BASE_DIR, repository)
    app = create_app(repository, engine)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    _wait_server(base)
    print(f"Q忆 已启动：{base}/console（档案：{base}/）")
    webbrowser.open(base + "/console")
    # 关控制台 tab 后无心跳超 20s 且非采集中则退出
    try:
        while True:
            time.sleep(5)
            status = engine.status().get("status")
            if status in ("running", "requesting", "stopping"):
                continue
            beat = getattr(app, "last_beat", None)
            if beat and time.time() - beat["t"] > 20:
                print("控制台页面已关闭，自动退出")
                break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
