import json
import re
from pathlib import Path

from app.config import bkn, parse_curl

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0")


def _strip_cont(text):
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\^\r?\n", " ", text)
    return text.strip()


def _split_cookies(pair_text):
    out = {}
    for pair in pair_text.split(";"):
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            out[k] = v
    return out


def _pick_uin(cookies, url="", referer=""):
    m = re.search(r"[?&]uin=(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"user\.qzone\.qq\.com/(\d+)", referer or "")
    if m:
        return m.group(1)
    u = cookies.get("uin", "")
    if u:
        return u.lstrip("o")
    return ""


def build_conf(cookies, uin, ua="", referer=""):
    if not cookies:
        raise ValueError("未解析出 cookie")
    g_tk = bkn(cookies.get("p_skey", "")) if cookies.get("p_skey") else None
    if not g_tk:
        raise ValueError("cookie 缺少 p_skey，无法计算 g_tk")
    if not uin:
        raise ValueError("未能从 cookie 中识别 QQ 号，请手动填写")
    if not referer:
        referer = f"https://user.qzone.qq.com/{uin}/infocenter?loginfrom=31"
    return {
        "cookies": cookies,
        "uin": uin,
        "g_tk": g_tk,
        "user_agent": ua or DEFAULT_UA,
        "referer": referer,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }


def _parse_curl(text):
    m = re.search(r"--url[ =](['\"])(.*?)\1", text)
    url = m.group(2) if m else None
    if not url:
        m = re.search(r"curl\s+(['\"])(.*?)\1", text)
        url = m.group(2) if m else None
    cookies = {}
    for m in re.finditer(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", text):
        cookies.update(_split_cookies(m.group(2)))
    if not cookies:
        raise ValueError("curl 命令里未找到 -b cookie")
    headers = {}
    for m in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", text):
        if ":" in m.group(2):
            k, v = m.group(2).split(":", 1)
            headers[k.strip().lower()] = v.strip()
    uin = _pick_uin(cookies, url or "", headers.get("referer", ""))
    return build_conf(cookies, uin, headers.get("user-agent", ""), headers.get("referer", ""))


def _parse_string(text):
    cookies = _split_cookies(text)
    uin = _pick_uin(cookies)
    return build_conf(cookies, uin)


def parse_cookie_text(text):
    text = _strip_cont(text or "")
    if not text:
        raise ValueError("cookie 内容为空")
    if text.startswith("curl"):
        return _parse_curl(text)
    return _parse_string(text)


DEFAULTS = {
    "cookies": {}, "uin": "", "g_tk": 0, "user_agent": DEFAULT_UA,
    "referer": "", "photos_dir": "output/imgs", "videos_dir": "output/videos",
    "data_dir": "output/datas", "download_photos": True, "download_videos": True,
    "target_uin": "", "source": "curl.txt",
}


def config_path(root):
    # config.json 随采集数据放 output/ 下（打包备份恢复一致）；旧版在根目录也兼容读
    return Path(root) / "output" / "config.json"


def load(root):
    path = config_path(root)
    if not path.exists():
        legacy = Path(root) / "config.json"
        if legacy.exists():
            path = legacy
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                return {**DEFAULTS, **cfg}
        except (OSError, ValueError):
            pass
    curlf = Path(root) / "curl.txt"
    if not curlf.exists():
        return dict(DEFAULTS)
    conf = parse_curl(str(curlf))
    return {**DEFAULTS, "cookies": conf["cookies"], "uin": conf["uin"],
            "g_tk": conf["g_tk"], "user_agent": conf["user_agent"],
            "referer": conf["referer"], "target_uin": conf["uin"]}


def save(root, cfg):
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def apply_cookie(root, text, target_uin=""):
    conf = parse_cookie_text(text)
    cfg = load(root)
    cfg.update({
        "cookies": conf["cookies"], "uin": conf["uin"], "g_tk": conf["g_tk"],
        "user_agent": conf["user_agent"], "referer": conf["referer"],
        "source": "manual",
    })
    if target_uin:
        cfg["target_uin"] = str(target_uin).strip()
    save(root, cfg)
    return conf
