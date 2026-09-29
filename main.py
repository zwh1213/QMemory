import atexit
import logging
import os
import signal
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

from app.account_manager import AccountManager
from app.web import create_app


def _app_dir():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QMemory"
    return Path(sys.executable).resolve().parent


BASE_DIR = _app_dir()

_PID_FILE = BASE_DIR / "qmemory.pid"


def _kill_existing():
    # 程序常驻不退，再次启动时顶掉上一次的旧实例
    try:
        old = int(_PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid():
        try:
            os.kill(old, signal.SIGTERM)
        except OSError:
            pass  # 旧进程已经没了
        time.sleep(0.6)  # 给旧实例一点收尾时间
    try:
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _cleanup_pid():
    try:
        _PID_FILE.unlink()
    except OSError:
        pass


atexit.register(_cleanup_pid)


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
    _kill_existing()
    lg = logging.getLogger("werkzeug")
    lg.setLevel(logging.INFO)
    lg.addFilter(_NoAccessLog())
    manager = AccountManager(BASE_DIR)
    app = create_app(manager)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    _wait_server(base)
    print(f"Q忆 已启动：{base}/console（档案：{base}/）")
    webbrowser.open(base + "/console")
    # 程序常驻：关页面不退出；再次启动程序会顶掉本实例；控制台点「退出程序」才退出
    while not getattr(app, "should_stop", False):
        time.sleep(1)


if __name__ == "__main__":
    main()
