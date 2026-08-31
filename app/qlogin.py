import base64
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from app import cookiemgr

APPID = "549000912"   # QZone web 登录
DAID = "5"
S_URL = "https://user.qzone.qq.com/"
U1 = S_URL  # requests 的 params 会自动 URL 编码，直接传原始地址

LOGIN_REFERER = ("https://xui.ptlogin2.qq.com/cgi-bin/xlogin?"
                 "appid=549000912&daid=5&s_url=" + quote(S_URL, safe=""))


def _ptqrtoken(qrsig):
    # ptqrtoken 的 hash33（与 g_tk 的 hash33 不同：无 5381 种子）
    e = 0
    for ch in qrsig:
        e += (e << 5) + ord(ch)
    return 2147483647 & e


class QrLogin:
    def __init__(self, root):
        self.root = Path(root)
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self):
        s = requests.Session()
        s.headers.update({"User-Agent": cookiemgr.DEFAULT_UA, "Referer": LOGIN_REFERER})
        r = s.get("https://ssl.ptlogin2.qq.com/ptqrshow", params={
            "appid": APPID, "e": "2", "l": "M", "s": "3", "d": "72",
            "v": "4", "t": time.time(), "daid": DAID,
        }, timeout=15)
        r.raise_for_status()
        qrsig = s.cookies.get("qrsig")
        if not qrsig:
            raise ValueError("获取二维码失败：未返回 qrsig")
        qr_id = secrets.token_hex(8)
        token = _ptqrtoken(qrsig)
        with self._lock:
            self._sessions[qr_id] = {"session": s, "token": token, "created": time.time()}
        image = "data:image/gif;base64," + base64.b64encode(r.content).decode()
        return {"qr_id": qr_id, "image": image}

    def poll(self, qr_id):
        with self._lock:
            st = self._sessions.get(qr_id)
        if not st:
            return {"state": "expired", "error": "二维码已失效，请重新获取"}
        s = st["session"]
        r = s.get("https://ssl.ptlogin2.qq.com/ptqrlogin", params={
            "u1": U1, "ptqrtoken": st["token"], "ptredirect": "0", "h": "1",
            "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": "24082614", "js_type": "1", "login_sig": "",
            "pt_uistyle": "40", "aid": APPID, "daid": DAID, "o1v": "1",
        }, timeout=15)
        m = re.search(r"ptuiCB\('(\d+)','([^']*)','([^']*)','([^']*)','([^']*)'(?:,'([^']*)')?", r.text)
        if not m:
            return {"state": "error",
                    "error": "登录接口返回异常：" + r.text[:120]}
        code, result, url, uin, status_text, nick = m.groups()
        nick = nick or ""
        if code == "65":
            with self._lock:
                self._sessions.pop(qr_id, None)
            return {"state": "expired", "error": "二维码已过期，请重新获取"}
        if code == "66":
            return {"state": "waiting"}
        if code in ("67", "68"):
            return {"state": "scanned"}
        if code != "0":
            return {"state": "error", "error": f"登录失败（错误码 {code}）"}
        with self._lock:
            self._sessions.pop(qr_id, None)
        # ptuiCB 第 4 参数 uin 常为 '0'，真实 uin 在 check_sig URL 里
        m_uin = re.search(r"[?&]uin=(\d+)", url)
        if m_uin:
            uin = m_uin.group(1)
        uin = uin.lstrip("o")
        # 成功：follow check_sig URL（带 ptsigx）换取 p_skey
        try:
            s.get(url, timeout=15)
        except Exception:
            pass
        cookies = {c.name: c.value for c in s.cookies}
        if not cookies.get("p_skey") and cookies.get("skey"):
            try:
                s.get("https://ssl.ptlogin2.qq.com/jump", params={
                    "ptlang": "2052", "clientuin": uin, "clientkey": cookies.get("skey"),
                    "keyindex": "19", "daid": DAID, "u1": U1,
                }, timeout=15)
                cookies = {c.name: c.value for c in s.cookies}
            except Exception:
                pass
        cookies["uin"] = uin
        if not cookies.get("p_skey"):
            return {"state": "error", "error": "登录成功但未拿到 p_skey，请改用手动粘贴"}
        try:
            conf = cookiemgr.build_conf(
                cookies, uin,
                referer=f"https://user.qzone.qq.com/{uin}/infocenter?loginfrom=31")
        except ValueError as exc:
            return {"state": "error", "error": str(exc)}
        return {"state": "ok", "ok": True, "uin": conf["uin"], "g_tk": conf["g_tk"],
                "nickname": nick, "conf": conf}
