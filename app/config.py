# g_tk(bkn) 算法 + curl 命令解析
import re
from pathlib import Path


def bkn(key: str) -> int:
    # g_tk 算法：p_skey 逐字符 hash，取 31 位
    h = 5381
    for c in key:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


def _strip_continuations(text: str) -> str:
    # 去掉 bash(行尾 \\ ) 和 cmd(行尾 ^ ) 续行符
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\^\r?\n", " ", text)
    return text.strip()


def _first_quoted(block: str):
    m = re.search(r"'([^']*)'|\"([^\"]*)\"", block)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def parse_curl(path: str = "curl.txt") -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    text = _strip_continuations(raw)
    if not text.startswith("curl"):
        raise ValueError(f"{path} 不是 curl 命令（应以 curl 开头）")

    # url：--url '...' 或 curl 后第一个引号串
    m = re.search(r"--url[ =](['\"])(.*?)\1", text)
    url = m.group(2) if m else None
    if not url:
        m = re.search(r"curl\s+(['\"])(.*?)\1", text)
        url = m.group(2) if m else None

    # cookie：-b '...'
    cookies = {}
    for m in re.finditer(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", text):
        for pair in m.group(2).split(";"):
            if "=" in pair:
                k, v = pair.strip().split("=", 1)
                cookies[k] = v
    if not cookies:
        raise ValueError("未找到 -b 里的 cookie，请检查 curl.txt")

    # headers：-H 'name: value'
    headers = {}
    for m in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", text):
        if ":" in m.group(2):
            k, v = m.group(2).split(":", 1)
            headers[k.strip().lower()] = v.strip()

    # uin：url 参数优先，其次 referer，最后 cookie
    uin = None
    m = re.search(r"[?&]uin=(\d+)", url or "")
    if m:
        uin = m.group(1)
    else:
        m = re.search(r"user\.qzone\.qq\.com/(\d+)", headers.get("referer", ""))
        if m:
            uin = m.group(1)
    if not uin and "uin" in cookies:
        uin = cookies["uin"].lstrip("o")

    # g_tk：优先用 p_skey 现算（更可靠），其次用 url 里带的
    pskey = cookies.get("p_skey", "")
    g_tk = bkn(pskey) if pskey else None
    m = re.search(r"[?&]g_tk=(\d+)", url or "")
    url_g_tk = int(m.group(1)) if m else None
    if g_tk is None:
        g_tk = url_g_tk

    return {
        "url": url,
        "uin": uin,
        "g_tk": g_tk,
        "url_g_tk": url_g_tk,
        "user_agent": headers.get("user-agent", ""),
        "referer": headers.get("referer", ""),
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "skey": cookies.get("skey", ""),
        "p_skey": pskey,
    }


if __name__ == "__main__":
    conf = parse_curl()
    print("uin         =", conf["uin"])
    print("g_tk 计算    =", conf["g_tk"])
    print("g_tk 原始url =", conf["url_g_tk"])
    print("cookie 数量  =", len(conf["cookies"]))
    print("UA          =", conf["user_agent"][:60])
    print("referer     =", conf["referer"])
    if conf["g_tk"] and conf["url_g_tk"] and conf["g_tk"] != conf["url_g_tk"]:
        print("警告: 计算的 g_tk 与 curl 不一致，以计算值为准")
