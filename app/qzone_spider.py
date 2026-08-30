import hashlib
import json
import random
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from app.config import parse_curl

MOBILE_FEEDS_URL = "https://mobile.qzone.qq.com/get_feeds"
PC_FEEDS_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds2_html_pav_all"

CONFIG = {
    "curl_file": "curl.txt",
    "out_dir": "output",
    "feeds_limit": 0,        # 0=抓全部历史；先看看跑法就设成 40
    "request_delay": 3.5,    # 请求间隔秒，被风控就调大
    "save_media": False,     # True=图片视频下载到 media/；False=data.json 里只存链接
    "fetch_my_moods": True,      # True=自己现有帖子用 mood 列表补细节：多图/真实时间/转发卡/全评论
    "fresh": False,          # True=清空输出目录重新抓
    "crawl": True,           # True=继续抓 feeds 历史；False=用现有 feeds 数据重建，先验证再跑全量
    "pc_start": 0,           # 0=PC 从最新开始，不设历史上限
    "pc_resume": False,      # False=本轮 PC 从 pc_start 重新跑
    "pc_only": False,        # True=只跑 PC，不合并 mobile 原始结果
}


# ---------- 基础 ----------

class BlockedError(RuntimeError):
    pass


def check(data):
    code = data.get("code")
    if code == 0 or code is None:
        return data
    raise RuntimeError(f"接口错误 {code}: {data.get('message') or data.get('msg') or ''}")


def clean_text(text):
    text = re.sub(r"@\{uin:\d+,nick:([^,}]+)(?:,[^}]*)?\}", r"@\1", text)
    text = re.sub(r"@\{nick:([^,}]+)(?:,[^}]*)?\}", r"@\1", text)
    return re.sub(r"^：+", "", text or "").strip()


def avatar_url(uin):
    return f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"


def decode_ptnick(cookies, uin):
    # cookie 里 ptnick_<uin> 是 UTF-8 hex 编码的昵称，本地解码兜底用
    for key in (f"ptnick_{uin}", f"ptnick_o{uin}"):
        val = (cookies or {}).get(key)
        if val and re.fullmatch(r"[0-9a-fA-F]+", str(val)):
            try:
                nick = bytes.fromhex(str(val)).decode("utf-8", "ignore").strip()
                if nick:
                    return nick
            except ValueError:
                pass
    return ""


_NICK_CACHE = {}


def fetch_nickname(cfg, uin, use_cache=True):
    # 解析当前登录账号昵称：config 值 → ptnick 解码 → 接口请求（请求不保存，仅返回）
    key = f"{uin}:{cfg.get('source') or ''}"
    if use_cache and _NICK_CACHE.get(key):
        return _NICK_CACHE[key]
    nick = str(cfg.get("nickname") or "").strip()
    if nick and nick != "登录成功！":
        _NICK_CACHE[key] = nick
        return nick
    nick = decode_ptnick(cfg.get("cookies") or {}, uin)
    if not nick:
        try:
            s = requests.Session()
            s.headers.update({
                "User-Agent": cfg.get("user_agent") or "Mozilla/5.0",
                "Referer": f"https://user.qzone.qq.com/{uin}/",
            })
            s.cookies.update(cfg.get("cookies") or {})
            r = s.get("https://users.qzone.qq.com/fcg-bin/cgi_get_profile.fcg",
                      params={"uins": uin}, timeout=15)
            m = re.search(r"\((\{.*\})\)", r.text, re.S)
            if m:
                data = json.loads(m.group(1))
                info = data.get(str(uin))
                if isinstance(info, list) and info:
                    nick = str(info[0] or "")
        except Exception:
            pass
    if nick:
        _NICK_CACHE[key] = nick
    return nick


def user_info(u):
    if not isinstance(u, dict):
        return {"uin": "", "nickname": "", "avatar": ""}
    return {"uin": u.get("uin"), "nickname": u.get("nickname"),
            "avatar": avatar_url(u.get("uin")) if u.get("uin") else ""}


class Qzone:
    def __init__(self, conf, delay=0.6, jitter=0.5):
        self.conf = conf
        self.delay = delay
        self.jitter = jitter
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": conf["user_agent"],
            "Referer": conf["referer"],
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.s.cookies.update(conf["cookies"])

    def get(self, url, params=None, referer=None):
        headers = {"Referer": referer} if referer else None
        for i in range(6):
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=30)
                r.raise_for_status()
                r.content
                time.sleep(max(0, self.delay + random.uniform(-self.jitter, self.jitter)))
                return r
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                print(f"  请求失败重试({i + 1}/6): HTTP {code}", flush=True)
                time.sleep(min(2 ** i, 30) + random.uniform(0, 3))
            except Exception as e:
                print(f"  请求失败重试({i + 1}/6): {type(e).__name__}", flush=True)
                time.sleep(min(2 ** i, 30) + random.uniform(0, 3))
        raise RuntimeError("请求多次失败")


MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",
}


def _image_ext(head):
    for sig, ext in MAGIC.items():
        if head.startswith(sig):
            return ext
    return None


def download(session, url, dest_dir, name, referer, kind="image"):
    """下载到 dest_dir/{name}.{ext}，已存在则跳过。成功返回文件名，失败返回空串。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in dest_dir.glob(f"{name}.*"):
        if f.stat().st_size > 0:
            return f.name
    for i in range(3):
        try:
            r = session.get(url, headers={"Referer": referer}, timeout=40)
            if r.status_code != 200 or not r.content:
                continue
            head = r.content[:16]
            if kind == "image":
                ext = _image_ext(head)
                if ext:
                    fn = f"{name}.{ext}"
                    (dest_dir / fn).write_bytes(r.content)
                    return fn
            elif head[4:8] == b"ftyp" or (len(r.content) > 20000
                                          and not head.startswith(b"<")):
                fn = f"{name}.mp4"
                (dest_dir / fn).write_bytes(r.content)
                return fn
        except Exception:
            pass
        time.sleep(0.5 * (i + 1))
    return ""


# ---------- 媒体抽取 ----------

def photo_original(node):
    """从一张照片节点挑原图 URL：busi_param["-1"] 是原图，缩略图在 144/30 等键。"""
    bp = node.get("busi_param")
    if isinstance(bp, dict):
        for k in ("-1", "0"):
            u = bp.get(k)
            if isinstance(u, str) and u.startswith("http"):
                return u
    ph = node.get("photourl")
    if isinstance(ph, dict):
        for k in sorted(ph, key=lambda x: int(x) if str(x).isdigit() else 99):
            v = ph[k]
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                return v["url"]
    u = node.get("url")
    if isinstance(u, str) and u.startswith("http"):
        return u
    return ""


def collect_photos(node, out):
    """遍历 original，收集每张照片的原图 URL（跳过视频封面，视频单独处理）。"""
    if isinstance(node, dict):
        if isinstance(node.get("busi_param"), dict) or isinstance(node.get("photourl"), dict):
            u = photo_original(node)
            if u:
                out.append(u)
            return
        for k, v in node.items():
            if k == "cell_video":
                continue
            collect_photos(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_photos(v, out)


def post_media(o):
    """拆出帖子媒体：(原图URL去重, 视频[{url,cover}])。"""
    imgs = []
    collect_photos(o, imgs)
    imgs = list(dict.fromkeys(imgs))
    videos = []
    cv = o.get("cell_video")
    if isinstance(cv, dict):
        vu = cv.get("videourl")
        if isinstance(vu, str) and vu.startswith("http"):
            cover = ""
            cov = cv.get("coverurl")
            items = cov.values() if isinstance(cov, dict) else cov or []
            for c in items:
                if isinstance(c, dict) and isinstance(c.get("url"), str) \
                        and c["url"].startswith("http"):
                    cover = c["url"]
                    break
            videos.append({"url": vu, "cover": cover})
    return imgs, videos


# ---------- 抓取 ----------

def fetch_mobile(qz, refresh_type, attach):
    params = {"g_tk": qz.conf["g_tk"], "res_type": "1",
              "refresh_type": refresh_type, "format": "json"}
    if attach:
        params["res_attach"] = attach
    data = json.loads(qz.get(MOBILE_FEEDS_URL, params=params,
                             referer="https://h5.qzone.qq.com/").text)
    check(data)
    body = data.get("data") or {}
    feeds = body.get("vFeeds") or []
    attach = (body.get("attachinfo") or "").strip()
    has_more = bool(body.get("hasmore")) and bool(feeds) and bool(attach)
    return feeds, attach, has_more


def mobile_key(feed):
    comm = feed.get("comm") or {}
    skey = (comm.get("feedskey") or "").strip()
    if skey:
        return skey
    cell = (feed.get("original") or {}).get("cell_id") or {}
    if cell.get("cellid"):
        return str(cell["cellid"])
    likekey = (comm.get("curlikekey") or "").strip()
    if likekey:
        return likekey
    return hashlib.md5(json.dumps(feed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_state(outdir):
    f = outdir / "state.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def save_state(outdir, **kw):
    state = load_state(outdir)
    state.update(kw)
    (outdir / "state.json").write_text(json.dumps(state, ensure_ascii=False),
                                       encoding="utf-8")


def crawl_feeds(qz, outdir, limit):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    state = load_state(outdir)
    total = state.get("total", 0)
    seen = set(state.get("keys", []))
    attach = state.get("attach")
    page = state.get("page", 0)
    with open(outdir / "feeds.jsonl", "a", encoding="utf-8") as jl:
        reanchor = False
        dead = 0
        while True:
            rt = "1" if (page == 0 or reanchor) else "2"
            for attempt in range(3):
                try:
                    feeds, attach, has_more = fetch_mobile(qz, rt, None if reanchor else attach)
                    reanchor = False
                    break
                except Exception as e:
                    if attempt == 2:
                        reanchor = True
                        break
                    print(f"  第{page}页失败,{attempt + 1}/3 稍等 30s 重试... {type(e).__name__}")
                    time.sleep(30)
            if reanchor:
                dead += 1
                if dead >= 3:
                    raise RuntimeError(f"刷新窗口多次重置仍失败 第{page}页")
                print(f"  第{page}页连续失败,刷新窗口失效→重置从最新重走,已抓过的自动去重")
                time.sleep(10)
                continue
            dead = 0
            fresh = [f for f in feeds if mobile_key(f) not in seen]
            for f in fresh:
                seen.add(mobile_key(f))
                jl.write(json.dumps({"feed": f}, ensure_ascii=False) + "\n")
            total += len(fresh)
            page += 1
            print(f"第{page}页 页内{len(feeds)} 新增{len(fresh)} 累计{total}")
            save_state(outdir, page=page, total=total, attach=attach, keys=list(seen))
            if not has_more or (limit and total >= limit):
                break


# ---------- 帖子构建 ----------

def is_me(uin, my_uin):
    return bool(my_uin) and str(uin or "") == str(my_uin)


def parse_post(f, my_uin=None):
    o = f.get("original") or {}
    author = ((o.get("cell_userinfo") or {}).get("user") or {})
    imgs, videos = post_media(o)
    cc = o.get("cell_comment") or {}
    comments = []
    seen = set()
    for c in cc.get("comments") or []:
        cu = c.get("user") or {}
        item = {
            "uin": cu.get("uin"), "nickname": cu.get("nickname"),
            "avatar": avatar_url(cu.get("uin")) if cu.get("uin") else "",
            "content": clean_text(c.get("content") or ""),
            "time": c.get("date"),
            "is_mine": is_me(cu.get("uin"), my_uin),
            "replies": [],
        }
        seen.add((str(item["uin"]), item["content"], item["time"]))
        comments.append(item)
    main = cc.get("main_comment") or {}
    mu = main.get("user") or {}
    if main.get("content"):
        key = (str(mu.get("uin")), clean_text(main.get("content") or ""), main.get("date"))
        if key not in seen:
            comments.append({
                "uin": mu.get("uin"), "nickname": mu.get("nickname"),
                "avatar": avatar_url(mu.get("uin")) if mu.get("uin") else "",
                "content": clean_text(main.get("content") or ""),
                "time": main.get("date"),
                "is_mine": is_me(mu.get("uin"), my_uin),
                "replies": [],
            })
    op = (f.get("operation") or {}).get("busi_param") or {}
    op_msg = (op.get("184") or "") if isinstance(op, dict) else ""
    actor = (f.get("userinfo") or {}).get("user") or {}
    if op_msg and str(op_msg) != clean_text(main.get("content") or "") and actor.get("uin"):
        reply = {
            "uin": actor.get("uin"), "nickname": actor.get("nickname"),
            "avatar": avatar_url(actor.get("uin")) if actor.get("uin") else "",
            "content": clean_text(op_msg),
            "time": None,
            "is_mine": is_me(actor.get("uin"), my_uin),
        }
        target = next((c for c in comments if str(c["uin"]) == str(mu.get("uin"))), None)
        if target:
            target["replies"].append(reply)
    return {
        "id": (o.get("cell_id") or {}).get("cellid"),
        "type": "post",
        "time": (f.get("comm") or {}).get("time") or 0,
        "author": user_info(author),
        "is_mine": is_me(author.get("uin"), my_uin),
        "text": clean_text((o.get("cell_summary") or {}).get("summary") or ""),
        "images": imgs,
        "videos": videos,
        "comments": comments,
        "likes": [],
        "mentions": [],
    }


def parse_leave_msg(f, my_uin=None):
    """给我留言：数据在顶层 comment.comments，不在 original.cell_*。"""
    cc = f.get("comment") or {}
    msgs = cc.get("comments") or []
    author = {}
    text = ""
    comments = []
    post_time = (f.get("comm") or {}).get("time") or 0

    def item(c):
        u = c.get("user") or {}
        return {
            "uin": u.get("uin"), "nickname": u.get("nickname"),
            "avatar": avatar_url(u.get("uin")) if u.get("uin") else "",
            "content": clean_text(c.get("content") or ""),
            "time": c.get("date") or post_time,
            "is_mine": is_me(u.get("uin"), my_uin),
            "replies": [],
        }

    for i, c in enumerate(msgs):
        if i == 0:
            author = c.get("user") or {}
            text = c.get("content") or ""
        else:
            comments.append(item(c))
        for r in c.get("replys") or []:
            comments.append(item(r))
    o = f.get("original") or {}
    return {
        "id": (msgs[0].get("commentid") if msgs else "") or (o.get("cell_id") or {}).get("cellid"),
        "type": "post",
        "time": post_time,
        "author": user_info(author),
        "is_mine": is_me(author.get("uin"), my_uin),
        "text": clean_text(text),
        "images": [],
        "videos": [],
        "comments": comments,
        "likes": [],
        "mentions": [],
    }


def parse_notify(f):
    o = f.get("original") or {}
    comm = f.get("comm") or {}
    m = re.search(r"/mood/([0-9a-fA-F]+)", comm.get("curlikekey") or "")
    target = m.group(1) if m else ""
    cover = ""
    pd = (o.get("cell_pic") or {}).get("picdata") or {}
    for p in pd.get("pic") or []:
        if isinstance(p, dict):
            cover = photo_original(p)
            if cover:
                break
    return {
        "target": target,
        "actor": user_info((f.get("userinfo") or {}).get("user") or {}),
        "author": user_info((o.get("cell_userinfo") or {}).get("user") or {}),
        "cover": cover,
        "text": clean_text((o.get("cell_summary") or {}).get("summary") or ""),
        "time": comm.get("time") or 0,
    }


def merge_comments(a, b):
    """合并评论列表，按 uin+内容+时间去重，回复也合并。"""
    out = {}
    for c in a + b:
        k = (str(c.get("uin")), c.get("content"), c.get("time"))
        if k in out:
            out[k]["replies"] = merge_comments(out[k].get("replies") or [], c.get("replies") or [])
        else:
            out[k] = c
    return list(out.values())


# ---------- PC 信息中心墙 ----------
# feeds2_html_pav_all 返回 JSONP(_Callback 包裹的非严格 JS:裸 key/单引号/\xHH/undefined)
# offset 深翻页无封顶,覆盖 2026-08 → 2015-06,补 mobile 够不到的历史。


class JSLit:
    """纯 Python 解析 PC 墙 JSONP 的 JS 字面量。"""

    def __init__(self, s):
        self.s, self.i = s, 0

    def _skip(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def parse(self):
        self._skip()
        c = self.s[self.i]
        if c == "{":
            return self._obj()
        if c == "[":
            return self._arr()
        if c in "'\"":
            return self._str()
        m = re.match(r"true|false|null|undefined|-?\d+(\.\d+)?", self.s[self.i:])
        if not m:
            raise ValueError("JSLit 解析失败 @%d: %r" % (self.i, self.s[self.i:self.i + 50]))
        t = m.group(0)
        self.i += len(t)
        if t in ("true", "false"):
            return t == "true"
        if t in ("null", "undefined"):
            return None
        return float(t) if "." in t else int(t)

    def _obj(self):
        self.i += 1
        o = {}
        while True:
            self._skip()
            if self.s[self.i] == "}":
                self.i += 1
                return o
            if self.s[self.i] in "'\"":
                k = self._str()
            else:
                m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.s[self.i:])
                k, self.i = m.group(0), self.i + len(m.group(0))
            self._skip()
            if self.s[self.i] == ":":
                self.i += 1
                o[k] = self.parse()
            self._skip()
            if self.s[self.i] == ",":
                self.i += 1
            elif self.s[self.i] == "}":
                self.i += 1
                return o

    def _arr(self):
        self.i += 1
        a = []
        while True:
            self._skip()
            if self.s[self.i] == "]":
                self.i += 1
                return a
            a.append(self.parse())
            self._skip()
            if self.s[self.i] == ",":
                self.i += 1
            elif self.s[self.i] == "]":
                self.i += 1
                return a

    def _str(self):
        q = self.s[self.i]
        self.i += 1
        out = []
        while True:
            c = self.s[self.i]
            if c == "\\":
                n = self.s[self.i + 1]
                if n == "x":
                    out.append(chr(int(self.s[self.i + 2:self.i + 4], 16)))
                    self.i += 4
                elif n == "u":
                    out.append(chr(int(self.s[self.i + 2:self.i + 6], 16)))
                    self.i += 6
                elif n in "ntr":
                    out.append({"n": "\n", "t": "\t", "r": "\r"}[n])
                    self.i += 2
                elif n in "'\"":
                    out.append(n)
                    self.i += 2
                else:
                    out.append(n)
                    self.i += 2
            elif c == q:
                self.i += 1
                return "".join(out)
            else:
                out.append(c)
                self.i += 1


def parse_jslit(s):
    m = re.search(r"_Callback\((.*)\)\s*;?\s*$", s, re.S)
    return JSLit(m.group(1)).parse()


def _rg(p, s, g=1):
    m = re.search(p, s)
    return m.group(g) if m else ""


def hd_photo_url(url):
    # psc 缩略图 /m /s /b /n 等 → /q 原图；photo.store.qq.com 原图本身不带尺寸后缀，不动
    return re.sub(r"/([msbn])(?=&)", "/q", url.replace("\\/", "/"))


def fetch_pc(qz, uin, g_tk, offset):
    params = {"uin": uin, "begin_time": 0, "end_time": 0,
              "getappnotification": 1, "getnotifi": 1, "has_get_key": 0,
              "offset": offset, "set": 0, "count": 10, "useutf8": 1,
              "outputhtmlfeed": 1, "grz": 0.548485564191646, "scope": 1,
              "g_tk": g_tk}
    for attempt in range(3):
        r = qz.get(PC_FEEDS_URL, params=params)
        if "_Callback(" in r.text:
            d = parse_jslit(r.text)
            data = d.get("data") or {}
            main = data.get("main") or {}
            return data.get("data") or [], bool(main.get("hasMoreFeeds")), main.get("total_number") or main.get("totalFeeds") or main.get("totalCount") or main.get("feedCount")
        print(f"  offset={offset} 非 JSONP 响应(可能 WAF),30s 后重试 {attempt + 1}/3")
        time.sleep(30)
    raise BlockedError(f"offset={offset} 连续 3 次非 JSONP，疑似风控")


def parse_pc_card(f, my_uin=None):
    """一张 PC 墙卡片 → 结构化事件。无 data-tid(访问主页/赞相册等)返回 None。"""
    if not isinstance(f, dict):
        return None
    h = f.get("html") or ""
    tid = _rg(r'data-tid="([0-9a-fA-F]+)"', h)
    if not tid:
        return None
    feedstype = _rg(r'data-feedstype="(\d+)"', h)
    abstime = int(_rg(r'data-abstime="(\d+)"', h) or 0)
    author_uin = _rg(r'data-uin="(\d+)"', h)
    actor_uin = _rg(r'id="fct_(\d+)_', h)
    actor_nick = _rg(r'class="f-name[^"]*"[^>]*>([^<]+)</a>', h)
    action = _rg(r'class="\s*ui-mr10 state\s*">\s*([^<]+?)\s*</span>', h) or feedstype
    tb = re.search(r'<p class="txt-box-title[^"]*">(.*?)</p>', h, re.S)
    author_name, text = "", ""
    if tb:
        m = re.search(r'link="nameCard_%s"[^>]*>\s*([^<]+?)\s*</a>' % author_uin, tb.group(1))
        author_name = (m.group(1) if m else "").lstrip("@")
        seg = re.sub(r'<a class="nickname name[^>]*>.*?</a>', "", tb.group(1), flags=re.S)
        seg = re.sub(r'<span class="ellipsis-front">(.*?)</span>', r"\1", seg, flags=re.S)
        seg = re.sub(r"<span[^>]*>.*?</span>", "", seg, flags=re.S)
        seg = re.sub(r"<[^>]+>", "", seg).replace("&nbsp;", " ").replace("&amp;", "&")
        text = re.sub(r"\s+", " ", seg).strip()
    imgs = []
    seen_imgs = set()
    for u in re.findall(r"trueSrc:\s*'([^']+)'", h):
        u = hd_photo_url(u)
        if u not in seen_imgs:
            seen_imgs.add(u)
            imgs.append(u)
    for u in re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', h, re.I):
        u = hd_photo_url(u)
        low = u.lower()
        if "qlogo" in low or "qzonestyle" in low or low.startswith("/ac/"):
            continue
        if "qpic.cn/psc" not in low and "photo.store.qq.com" not in low:
            continue
        if u not in seen_imgs:
            seen_imgs.add(u)
            imgs.append(u)
    videos = []
    for _m in re.finditer(r"<video\b[^>]*>", h, re.I):
        _tag = _m.group(0)
        _mu = re.search(r'url3="([^"]+)"', _tag)
        if not _mu:
            continue
        _url = _mu.group(1).replace("&amp;", "&")
        _cover = ""
        _mc = re.search(r'poster="([^"]+)"', _tag, re.I)
        if _mc:
            _cover = hd_photo_url(_mc.group(1).replace("&amp;", "&"))
        if _url not in {v["url"] for v in videos}:
            videos.append({"url": _url, "cover": _cover})
    if not videos:
        # 兜底：url3 不在 <video> 标签内时退回纯 URL 列表
        videos = list(dict.fromkeys(v.replace("&amp;", "&") for v in re.findall(r'url3="([^"]+)"', h)))
    comment = None
    cm = re.search(r'class="comments-item[^"]*"[^>]*data-uin="(\d+)" data-nick="([^"]*)"'
                   r'.*?<a class="nickname[^"]*"[^>]*>[^<]*</a>[^:]*:?\s*(.*?)<div class="comments-op">',
                   h, re.S)
    if cm:
        content = re.sub(r"<[^>]+>", "", cm.group(3)).replace("&nbsp;", " ").replace("&amp;", "&")
        comment = {"uin": cm.group(1), "nickname": cm.group(2),
                   "content": clean_text(re.sub(r"\s+", " ", content).strip()),
                   "time": abstime,
                   "replies": []}
        # 父评论下的回复：嵌套在 mod-comments-sub 容器里的 replyroot
        sub = re.search(r'<div class="comments-list mod-comments-sub">(.*?)</ul>\s*</div>',
                        h[cm.end():], re.S)
        if sub:
            for item in re.findall(r'<li class="comments-item[^"]*"[^>]*data-type="replyroot"[^>]*data-uin="(\d+)"[^>]*data-nick="([^"]*)"[^>]*>(.*?)</li>',
                                   sub.group(1), re.S):
                rc = re.search(r"&nbsp;\s*:\s*(.*?)<div class=\"comments-op\">", item[2], re.S)
                rbody = rc.group(1) if rc else item[2]
                rbody = re.sub(r"<[^>]+>", "", rbody).replace("&nbsp;", " ").replace("&amp;", "&")
                comment["replies"].append({"uin": item[0], "nickname": item[1],
                                           "content": clean_text(re.sub(r"\s+", " ", rbody).strip()),
                                           "time": abstime})
    return {"tid": tid, "feedstype": feedstype, "abstime": abstime,
            "author_uin": author_uin, "author_name": author_name,
            "actor_uin": actor_uin, "actor_nick": actor_nick, "action": action,
            "text": text, "images": imgs, "videos": videos, "comment": comment}


def crawl_pc(qz, outdir, uin, g_tk, resume=True, limit=0, start=0):
    """PC 墙 offset 深翻页,卡片存 pc_cards.jsonl。断点记 pc_state.json,
    resume=True 时自动从断点继续(换 cookie 后重跑即续,不用改配置)。"""
    path = Path(outdir) / "pc_cards.jsonl"
    state_path = Path(outdir) / "pc_state.json"
    n, offset = 0, start
    if resume and state_path.exists():
        try:
            offset = int(json.loads(state_path.read_text(encoding="utf-8")).get("offset") or start)
            print(f"  PC 断点续跑 offset={offset}")
        except Exception:
            pass
    while True:
        cards, has_more, total_hint = fetch_pc(qz, uin, g_tk, offset)
        if total_hint is not None and offset == start:
            print(f"  PC 接口返回 total={total_hint}")
        if not cards:
            print(f"  PC offset={offset} 空页,结束")
            break
        with open(path, "w" if offset == start else "a", encoding="utf-8") as fh:
            for f in cards:
                c = parse_pc_card(f, uin)
                if c:
                    fh.write(json.dumps(c, ensure_ascii=False) + "\n")
                    n += 1
        offset += len(cards)
        state_path.write_text(json.dumps({"offset": offset}, ensure_ascii=False), encoding="utf-8")
        if offset % 100 == 0:
            print(f"  PC offset={offset} 累计 {n} 卡")
        if not has_more or (limit and n >= limit):
            break
    if not has_more:
        try:
            state_path.unlink()
        except OSError:
            pass
    print(f"PC 墙抓取: {n} 张卡片,offset={offset} → {path}")
    return n


def merge_pc(posts, outdir, my_uin=None):
    """PC 卡片按 tid 归并成帖子并进现有 posts;mobile 已有的只补赞/评,没有的整帖新增。"""
    path = Path(outdir) / "pc_cards.jsonl"
    if not path.exists():
        return posts
    by_tid = {}
    for line in open(path, encoding="utf-8"):
        c = json.loads(line)
        d = by_tid.setdefault(c["tid"], {"cards": [], "min": 10 ** 18})
        d["cards"].append(c)
        if c["abstime"]:
            d["min"] = min(d["min"], c["abstime"])
    existing = {str(p.get("id") or ""): p for p in posts}
    for tid, d in by_tid.items():
        cards = d["cards"]
        likes, comments, images, videos, text = [], [], [], [], ""
        for c in cards:
            if c["images"] and not images:
                images = c["images"]
            if c["videos"] and not videos:
                videos = c["videos"]
            if c["text"] and not text:
                text = c["text"]
            if c["feedstype"] == "101" or "赞" in (c["action"] or ""):
                if c["actor_uin"]:
                    likes.append({"uin": c["actor_uin"], "nickname": c["actor_nick"],
                                  "avatar": avatar_url(c["actor_uin"])})
            elif c["comment"]:
                cc = c["comment"]
                comments.append({"uin": cc["uin"], "nickname": cc["nickname"],
                                 "avatar": avatar_url(cc["uin"]) if cc["uin"] else "",
                                 "content": cc["content"], "time": cc["time"] or c["abstime"],
                                 "is_mine": is_me(cc["uin"], my_uin), "replies": []})
        likes = list({(x["uin"], x["nickname"]): x for x in likes}.values())
        comments = merge_comments([], comments)
        first = cards[0]
        post = {"id": tid, "type": "post", "time": d["min"] if d["min"] < 10 ** 18 else first["abstime"],
                "author": user_info({"uin": first["author_uin"], "nickname": first["author_name"]}),
                "is_mine": is_me(first["author_uin"], my_uin),
                "text": text, "images": images, "videos": videos,
                "comments": comments, "likes": likes, "mentions": []}
        if tid in existing:
            old = existing[tid]
            old["likes"] = list({(x["uin"], x["nickname"]): x for x in (old.get("likes") or []) + likes}.values())
            old["comments"] = merge_comments(old.get("comments") or [], comments)
        else:
            existing[tid] = post
    return sorted(existing.values(), key=lambda p: p.get("time") or 0, reverse=True)


def build_posts(feeds_dir, my_uin=None):
    """把 feeds.jsonl 转成帖子数组：点赞/提到按帖子 id 归并，评论合并去重。"""
    posts = {}
    for line in open(Path(feeds_dir) / "feeds.jsonl", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)["feed"]
        except Exception:
            continue
        if not isinstance(f, dict):
            continue
        title = (f.get("title") or {}).get("title") or ""
        if title in ("赞了", "赞了我", "提到我"):
            info = parse_notify(f)
            pid = info["target"]
            if not pid:
                continue
            post = posts.setdefault(pid, {
                "id": pid, "type": "like", "time": info["time"],
                "author": info["author"], "is_mine": is_me((info["author"] or {}).get("uin"), my_uin),
                "text": info["text"], "images": [], "videos": [],
                "comments": [], "likes": [], "mentions": [],
            })
            if info["cover"] and not post["images"]:
                post["images"].append(info["cover"])
            if title == "提到我":
                post["mentions"].append(info["actor"])
            else:
                post["likes"].append(info["actor"])
        else:
            post = parse_leave_msg(f, my_uin) if title == "给我留言" else parse_post(f, my_uin)
            pid = post["id"]
            if not pid:
                continue
            if pid in posts:
                old = posts.pop(pid)
                post["likes"] = old.get("likes", [])
                post["mentions"] = old.get("mentions", [])
                post["comments"] = merge_comments(old.get("comments", []), post["comments"])
            posts[pid] = post
    # 纯点赞通知(被赞帖子已无任何可还原内容)不作为独立帖子
    result = [p for p in posts.values()
              if not (p.get("type") == "like"
                      and not p.get("text") and not p.get("images") and not p.get("videos"))]
    return sorted(result, key=lambda p: p.get("time") or 0, reverse=True)


def _image_alive(u):
    """qzone 占位图(照片已删)是极小 GIF,视为死链;网络异常不误删。"""
    try:
        r = requests.get(u, timeout=10)
        b = r.content
        return not (len(b) < 3000 and b[:6] in (b"GIF89a", b"GIF87a"))
    except Exception:
        return True


def filter_dead_images(posts):
    """去掉已失效的封面直链(照片随原帖删除),避免破图。"""
    urls = {u for p in posts for u in (p.get("images") or [])}
    alive = {}
    if urls:
        with ThreadPoolExecutor(max_workers=16) as ex:
            for u, ok in zip(urls, ex.map(_image_alive, urls)):
                alive[u] = ok
    for p in posts:
        if p.get("images"):
            p["images"] = [u for u in p["images"] if alive.get(u, True)]
    return posts


# ---------- 下载 ----------

def download_all(posts, qz, out):
    media = Path(out) / "media"
    photos_dir = media / "photos"
    videos_dir = media / "videos"
    avatars_dir = media / "avatars"
    photo_map = {}
    video_map = {}
    avatar_map = {}

    def dl_photo(url):
        if url in photo_map:
            return photo_map[url]
        name = hashlib.md5(url.encode()).hexdigest()
        fn = download(qz.s, url, photos_dir, name, qz.conf["referer"], "image")
        photo_map[url] = f"media/photos/{fn}" if fn else ""
        return photo_map[url]

    def dl_video(url):
        if url in video_map:
            return video_map[url]
        name = hashlib.md5(url.encode()).hexdigest()
        fn = download(qz.s, url, videos_dir, name, qz.conf["referer"], "video")
        video_map[url] = f"media/videos/{fn}" if fn else ""
        return video_map[url]

    def dl_avatar(uin):
        if not uin:
            return ""
        key = str(uin)
        if key in avatar_map:
            return avatar_map[key]
        fn = download(qz.s, avatar_url(uin), avatars_dir, key,
                      "https://user.qzone.qq.com/", "image")
        avatar_map[key] = f"media/avatars/{fn}" if fn else ""
        return avatar_map[key]

    for post in posts:
        post["author"]["avatar"] = dl_avatar(post["author"]["uin"])
        for l in post["likes"]:
            l["avatar"] = dl_avatar(l["uin"])
        for c in post["comments"]:
            c["avatar"] = dl_avatar(c["uin"])
        for m in post["mentions"]:
            m["avatar"] = dl_avatar(m["uin"])
        imgs = []
        for u in post["images"]:
            p = dl_photo(u)
            if p:
                imgs.append(p)
        post["images"] = imgs
        for v in post["videos"]:
            v["path"] = dl_video(v["url"])
            v["cover"] = dl_photo(v["cover"]) if v.get("cover") else ""
    print(f"媒体下载完成，共 {len(photo_map) + len(video_map) + len(avatar_map)} 个唯一文件 → {media}")


MY_MOODS_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"


def fetch_my_moods(qz, uin):
    """翻页拉自己的全部说说。返回 {tid: mood}。只作细节补充，已删除帖这里没有，维持 feed 原样。"""
    moods = {}
    pos = 0
    while True:
        params = {"uin": uin, "ftype": 0, "sort": 0, "pos": pos, "num": 30,
                  "replynum": 100, "g_tk": qz.conf["g_tk"], "callback": "_preloadCallback",
                  "code_version": 1, "format": "jsonp", "need_private_comment": 1}
        try:
            r = qz.get(MY_MOODS_URL, params=params,
                       referer=f"https://user.qzone.qq.com/{uin}/mood")
            m = re.search(r"_preloadCallback\((.*)\)\s*;?\s*$", r.text, re.S)
            d = json.loads(m.group(1)) if m else {}
        except Exception:
            break
        if d.get("code") != 0:
            break
        ms = d.get("msglist") or []
        if not ms:
            break
        for mo in ms:
            moods[str(mo.get("tid"))] = mo
        pos += len(ms)
        if len(ms) < 30:
            break
    return moods


def mood_comments(mood, my_uin):
    """解 mood 的 commentlist 成评论树。作者在顶层 uin/name，回复在 list_3，
    回复内容开头的 @{...} 是"回复谁"，剥掉避免与展示的"回复 被回复人"重复。"""
    out = []
    for c in mood.get("commentlist") or []:
        replies = []
        for rp in c.get("list_3") or []:
            content = rp.get("content") or ""
            mt = re.search(r"@\{uin:\d+,nick:([^,}]+)", content)
            replies.append({
                "uin": rp.get("uin"), "nickname": rp.get("name"),
                "avatar": avatar_url(rp.get("uin")) if rp.get("uin") else "",
                "content": clean_text(re.sub(r"^\s*@\{[^}]*\}\s*", "", content)),
                "time": rp.get("create_time"),
                "is_mine": is_me(rp.get("uin"), my_uin),
                "reply": mt.group(1) if mt else "",
            })
        out.append({
            "uin": c.get("uin"), "nickname": c.get("name"),
            "avatar": avatar_url(c.get("uin")) if c.get("uin") else "",
            "content": clean_text(c.get("content") or ""),
            "time": c.get("create_time"),
            "is_mine": is_me(c.get("uin"), my_uin),
            "replies": replies,
        })
    return out


def apply_mood(post, mood, my_uin):
    """把 msglist 里自己帖子的权威细节合进 feed 构建的帖子：真实发帖时间、
    全文、全部图片、转发卡、完整评论。绝不新增帖子。"""
    post["time"] = mood.get("created_time") or post["time"]
    if mood.get("content"):
        post["text"] = mood["content"]
    pics = mood.get("pic") or []
    if pics:
        post["images"] = [p.get("url1") or p.get("smallurl")
                          for p in pics if p.get("url1") or p.get("smallurl")]
    rt = mood.get("rt_con")
    if isinstance(rt, dict) and (rt.get("content") or rt.get("conlist")):
        text = rt.get("content") or ""
        if not text:
            text = "".join(x.get("con") or "" for x in rt.get("conlist") or [])
        post["reshare"] = {
            "author": {"uin": mood.get("rt_uin") or "", "nickname": mood.get("rt_uinname") or "",
                       "avatar": avatar_url(mood.get("rt_uin")) if mood.get("rt_uin") else ""},
            "text": clean_text(text),
        }
    comments = mood_comments(mood, my_uin)
    if comments:
        post["comments"] = comments
    return post


def mood_to_detail(mood, my_uin):
    """mood（emotion_cgi_msglist_v6 一条）→ details.json 条目，供 repository 补评论/多图/时间/转发。"""
    d = {"time": mood.get("created_time") or 0}
    if mood.get("content"):
        d["text"] = mood["content"]
    pics = mood.get("pic") or []
    if pics:
        d["images"] = [hd_photo_url(p.get("url1") or p.get("smallurl"))
                       for p in pics if p.get("url1") or p.get("smallurl")]
    vids = mood.get("video") or []
    if vids:
        d["videos"] = [{"url": v.get("url3") or v.get("url") or v.get("video_url"),
                        "cover": hd_photo_url(v.get("pic_url") or v.get("url1") or v.get("cover") or "")}
                       for v in vids if v.get("url3") or v.get("url") or v.get("video_url")]
    rt = mood.get("rt_con")
    if isinstance(rt, dict) and (rt.get("content") or rt.get("conlist")):
        txt = rt.get("content") or ""
        if not txt:
            txt = "".join(x.get("con") or "" for x in rt.get("conlist") or [])
        d["reshare"] = {"author": {"uin": mood.get("rt_uin") or "",
                                   "nickname": mood.get("rt_uinname") or "",
                                   "avatar": avatar_url(mood.get("rt_uin")) if mood.get("rt_uin") else ""},
                        "text": clean_text(txt)}
    comments = mood_comments(mood, my_uin)
    if comments:
        d["comments"] = comments
    return d


VIEWER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Q忆</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Serif+SC:wght@600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  color-scheme:light;
  --bg:#f6efe9;
  --bg2:#f2e3db;
  --card:rgba(255,250,246,.62);
  --card2:rgba(255,243,235,.38);
  --card3:rgba(255,255,255,.18);
  --text:#241915;
  --muted:#7f675d;
  --soft:#d8c0b3;
  --line:rgba(79,48,38,.12);
  --accent:#d95d39;
  --accent2:#ff7a5c;
  --accent3:#ffb08f;
  --accent4:#f5d3c8;
  --shadow:0 22px 60px rgba(97,58,42,.12);
  --shadow2:0 10px 24px rgba(97,58,42,.08);
  --glass:blur(18px) saturate(165%);
}
:root:not([data-theme="light"]){
  --bg:#100f13;
  --bg2:#18161d;
  --card:rgba(24,23,33,.56);
  --card2:rgba(35,32,47,.34);
  --card3:rgba(255,255,255,.06);
  --text:#f8f2ee;
  --muted:#bda9a1;
  --soft:#5b4640;
  --line:rgba(255,233,225,.12);
  --accent:#ff8a63;
  --accent2:#d68dff;
  --accent3:#7dd6ff;
  --accent4:#6e4a40;
  --shadow:0 24px 60px rgba(0,0,0,.38);
  --shadow2:0 10px 24px rgba(0,0,0,.22);
  --glass:blur(18px) saturate(170%);
}
:root[data-theme="dark"]{
  --bg:#100f13;
  --bg2:#18161d;
  --card:rgba(24,23,33,.56);
  --card2:rgba(35,32,47,.34);
  --card3:rgba(255,255,255,.06);
  --text:#f8f2ee;
  --muted:#bda9a1;
  --soft:#5b4640;
  --line:rgba(255,233,225,.12);
  --accent:#ff8a63;
  --accent2:#d68dff;
  --accent3:#7dd6ff;
  --accent4:#6e4a40;
  --shadow:0 24px 60px rgba(0,0,0,.38);
  --shadow2:0 10px 24px rgba(0,0,0,.22);
  --glass:blur(18px) saturate(170%);
}
[data-palette="rose"]{
  --accent:#ff6b8a;--accent2:#ff9f68;--accent3:#ffc6a8;--accent4:#f7d3dc;
}
[data-palette="aurora"]{
  --accent:#6c7cff;--accent2:#27c7b8;--accent3:#91e8ff;--accent4:#cbd0ff;
}
[data-palette="purple"]{
  --accent:#8b5cf6;--accent2:#c084fc;--accent3:#e9d5ff;--accent4:#d8c4f2;
}
[data-palette="mint"]{
  --accent:#00a889;--accent2:#55c98b;--accent3:#b0e7c8;--accent4:#c5e5d8;
}
[data-palette="amber"]{
  --accent:#d97924;--accent2:#eab449;--accent3:#ffd88a;--accent4:#f1d7a2;
}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{
  font-family:Inter,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  background:radial-gradient(1200px 800px at 10% -10%, rgba(217,93,57,.22), transparent 42%),
             radial-gradient(900px 700px at 88% 2%, rgba(214,141,255,.20), transparent 44%),
             radial-gradient(700px 560px at 60% 110%, rgba(125,214,255,.14), transparent 42%),
             linear-gradient(180deg,var(--bg),var(--bg2));
  color:var(--text);
  font-size:14px;
  line-height:1.6;
}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.22;background:linear-gradient(135deg,rgba(255,255,255,.12),transparent 35%,rgba(255,255,255,.06) 65%,transparent);mix-blend-mode:screen}
body.loaded .shell{opacity:1;transform:none}
.shell{max-width:1180px;margin:0 auto;padding:24px 18px 48px;opacity:0;transform:translateY(12px);transition:opacity .5s ease, transform .5s ease}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 12px 18px}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.mark{width:44px;height:44px;border-radius:16px;flex:none;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent2),var(--accent3));color:#fff;font-size:22px;box-shadow:var(--shadow2)}
.brand h1{font-family:"Noto Serif SC",serif;font-size:22px;font-weight:800;letter-spacing:.02em;text-wrap:balance}
.brand p{color:var(--muted);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.search{min-width:240px;max-width:340px;width:min(38vw,340px);padding:11px 14px;border-radius:999px;border:1px solid var(--line);background:linear-gradient(135deg,var(--card),var(--card2));color:var(--text);box-shadow:var(--shadow2);backdrop-filter:var(--glass);outline:none}
.search::placeholder{color:var(--muted)}
.theme-btn,.pill{border:1px solid var(--line);background:linear-gradient(135deg,var(--card),var(--card2));color:var(--text);border-radius:999px;padding:10px 14px;cursor:pointer;box-shadow:var(--shadow2);backdrop-filter:var(--glass);transition:transform .18s ease, background .18s ease, border-color .18s ease}
.theme-btn:hover,.pill:hover,.chip:hover,.action:hover,.f-op a:hover{transform:translateY(-1px)}
.hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.9fr);gap:18px;align-items:stretch;margin-bottom:18px}
.hero-card,.side-card,.feed-card,.postbox,.story,.mask img{border:1px solid var(--line);background:linear-gradient(135deg,var(--card),var(--card2) 55%,var(--card3));box-shadow:var(--shadow);backdrop-filter:var(--glass)}
.hero-card{border-radius:28px;padding:28px;overflow:hidden;position:relative}
.hero-card:before{content:"";position:absolute;inset:18px;pointer-events:none;border-radius:24px;background:linear-gradient(135deg,rgba(255,255,255,.26),transparent 22%,rgba(255,255,255,.06) 60%,transparent 78%);mix-blend-mode:screen;opacity:.65}
.hero-card:after{content:"";position:absolute;inset:auto -16% -30% auto;width:260px;height:260px;border-radius:50%;background:radial-gradient(circle, rgba(217,93,57,.24), transparent 64%);pointer-events:none}
.kicker{display:inline-flex;align-items:center;gap:8px;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.kicker:before{content:"";width:10px;height:10px;border-radius:999px;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 0 0 6px rgba(217,93,57,.12)}
.hero h2{margin-top:14px;font-family:"Noto Serif SC",serif;font-size:clamp(30px,4vw,54px);line-height:1.03;letter-spacing:-.02em;max-width:11ch;text-wrap:balance}
.hero p{margin-top:14px;max-width:42ch;color:var(--muted);font-size:15px}
.hero-meta{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.hero-stat{min-width:120px;padding:14px 15px;border-radius:18px;background:linear-gradient(135deg,rgba(255,255,255,.20),rgba(255,255,255,.06));border:1px solid var(--line)}
.hero-stat b{display:block;font-size:20px;line-height:1.1;font-variant-numeric:tabular-nums}
.hero-stat span{font-size:12px;color:var(--muted)}
.side-card{border-radius:28px;padding:20px;display:flex;flex-direction:column;gap:16px;overflow:hidden}
.profile{display:flex;align-items:center;gap:14px}
.p-ava{width:74px;height:74px;border-radius:24px;object-fit:cover;flex:none;border:1px solid var(--line);background:var(--bg2)}
.p-meta{min-width:0;flex:1}
.p-name{font-size:20px;font-weight:800;letter-spacing:-.01em}
.p-qq{color:var(--muted);font-size:12px;margin-top:3px;font-variant-numeric:tabular-nums}
.story-row{display:flex;gap:10px;overflow:auto;padding-bottom:4px;scrollbar-width:none}
.story-row::-webkit-scrollbar{display:none}
.story{min-width:78px;border-radius:22px;padding:10px 10px 12px;text-align:center;box-shadow:none;background:linear-gradient(135deg, color-mix(in srgb,var(--card) 88%, transparent), color-mix(in srgb,var(--card2) 96%, transparent))}
.story .dot{width:34px;height:34px;border-radius:14px;margin:0 auto 8px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2),var(--accent3))}
.story span{display:block;font-size:12px;color:var(--muted)}
.chips{display:flex;gap:10px;flex-wrap:wrap}
.chip{padding:9px 14px;border-radius:999px;border:1px solid var(--line);background:linear-gradient(135deg,var(--card),var(--card2));color:var(--text);cursor:pointer;backdrop-filter:var(--glass);transition:transform .18s ease, border-color .18s ease, background .18s ease, color .18s ease}
.chip.on{background:linear-gradient(135deg,var(--accent),var(--accent2),var(--accent3));border-color:transparent;color:#fff}
.postbox{margin:18px 0 16px;border-radius:26px;padding:18px;display:flex;gap:14px;align-items:center}
.b-ava{width:44px;height:44px;border-radius:16px;object-fit:cover;flex:none}
.b-input{flex:1;border:1px solid var(--line);border-radius:18px;padding:13px 16px;color:var(--muted);background:linear-gradient(135deg,rgba(255,255,255,.16),rgba(255,255,255,.05));backdrop-filter:var(--glass)}
.actions{display:flex;gap:10px}
.action{width:42px;height:42px;border-radius:14px;border:1px solid var(--line);background:linear-gradient(135deg,rgba(255,255,255,.12),rgba(255,255,255,.04));color:var(--text);cursor:pointer;backdrop-filter:var(--glass);transition:transform .18s ease, background .18s ease}
.layout{display:grid;grid-template-columns:minmax(0,1.15fr) 320px;gap:18px;align-items:start}
.feed{display:flex;flex-direction:column;gap:16px;min-width:0}
.f-item{opacity:0;transform:translateY(14px) scale(.985);animation:rise .55s ease forwards}
@keyframes rise{to{opacity:1;transform:none}}
.f-item:nth-child(2n){animation-delay:.03s}
.f-item:nth-child(3n){animation-delay:.06s}
.f-box,.panel{border-radius:26px;padding:18px}
.f-box{border:1px solid var(--line);background:linear-gradient(135deg,var(--card),var(--card2) 56%,var(--card3));box-shadow:var(--shadow);backdrop-filter:var(--glass)}
.f-hd{display:flex;align-items:center;justify-content:space-between;gap:12px}
.author{display:flex;align-items:center;gap:12px;min-width:0}
.f-ava{width:48px;height:48px;border-radius:18px;object-fit:cover;flex:none;background:var(--bg2)}
.nname{color:var(--text);text-decoration:none;font-weight:700}
.f-hd .nname{font-size:15px}
.sub{font-size:12px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.tag{font-size:11px;padding:5px 9px;border-radius:999px;background:linear-gradient(135deg,rgba(255,255,255,.14),rgba(255,255,255,.04));color:var(--muted);border:1px solid var(--line)}
.tag.me{background:linear-gradient(135deg,rgba(217,93,57,.16),rgba(255,122,92,.12));color:var(--accent);border-color:rgba(217,93,57,.18)}
.f-content{margin-top:14px}
.f-text{font-size:15px;line-height:1.72;word-break:break-word;white-space:pre-wrap}
.f-text .emoji,.c-text .emoji{width:18px;height:18px;vertical-align:-3px;margin:0 1px}
.f-grid{margin-top:14px;display:grid;gap:8px}
.f-grid img,.f-video video,.f-share{border-radius:18px}
.f-grid img{width:100%;height:100%;object-fit:cover;cursor:zoom-in;background:var(--bg2);transition:transform .24s ease, filter .24s ease}
.f-grid img:hover{transform:scale(1.015);filter:saturate(1.04)}
.f-grid.one{grid-template-columns:1fr;max-width:360px}
.f-grid.one img{max-height:420px;object-fit:contain}
.f-grid.two{grid-template-columns:repeat(2,1fr)}
.f-grid.two img{height:220px}
.f-grid.three{grid-template-columns:repeat(3,1fr)}
.f-grid.three img{height:150px}
.f-video{margin-top:14px}
.f-video video{width:100%;background:#000;max-height:420px}
.f-share{margin-top:14px;display:flex;gap:12px;padding:14px;border:1px solid var(--line);background:linear-gradient(135deg,rgba(255,255,255,.14),rgba(255,255,255,.04));backdrop-filter:var(--glass)}
.s-ava{width:38px;height:38px;border-radius:14px;flex:none;object-fit:cover;background:var(--bg2)}
.s-bd{flex:1;min-width:0;font-size:13px;line-height:1.6;color:var(--muted);word-break:break-word}
.s-bd .nname{font-size:13px}
.note{display:inline-flex;align-items:center;gap:6px;margin-top:12px;font-size:13px;color:var(--accent);background:linear-gradient(135deg,rgba(217,93,57,.14),rgba(255,122,92,.10));padding:6px 12px;border-radius:999px}
.f-foot{margin-top:14px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.f-info{font-size:12px;color:var(--muted)}
.f-info a{color:var(--muted);text-decoration:none}
.f-op{display:flex;gap:8px;flex-wrap:wrap}
.f-op a{display:flex;align-items:center;gap:4px;font-size:13px;color:var(--text);padding:8px 10px;border-radius:999px;text-decoration:none;background:linear-gradient(135deg,rgba(255,255,255,.14),rgba(255,255,255,.04));border:1px solid var(--line);backdrop-filter:var(--glass);transition:transform .18s ease, border-color .18s ease}
.f-op a.like.on{border-color:rgba(217,93,57,.2);color:var(--accent)}
.f-op em{font-style:normal;color:var(--muted);margin-left:2px}
.f-like{margin-top:14px;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--muted);display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap}
.f-like i{font-style:normal;color:var(--accent)}
.f-like .names{color:var(--text)}
.f-comments{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.c-item{display:flex;gap:10px;padding:8px 0}
.c-ava{width:34px;height:34px;border-radius:13px;object-fit:cover;flex:none;background:var(--bg2)}
.avi{display:inline-flex;align-items:center;justify-content:center;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.c-bd{flex:1;min-width:0}
.c-text{font-size:14px;line-height:1.62;word-break:break-word;color:var(--text)}
.c-text .nname{font-size:14px}
.c-text .colon{color:var(--soft)}
.rep{font-style:normal;color:var(--muted);font-size:13px}
.c-meta{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-top:4px}
.c-meta a{color:var(--muted);text-decoration:none}
.c-sub{margin-top:4px;padding-left:14px;border-left:2px solid var(--line)}
.panel{border:1px solid var(--line);background:linear-gradient(180deg,var(--card),var(--card2));box-shadow:var(--shadow);position:sticky;top:18px}
.panel h3{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.timeline{margin-top:14px;display:flex;flex-direction:column;gap:12px}
.t-item{display:flex;align-items:flex-start;gap:10px}
.t-dot{width:10px;height:10px;border-radius:999px;background:var(--accent);margin-top:7px;box-shadow:0 0 0 6px rgba(217,93,57,.12)}
.t-bd{min-width:0}
.t-bd b{display:block;font-size:14px}
.t-bd span{display:block;font-size:12px;color:var(--muted)}
.mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:99;align-items:center;justify-content:center;padding:24px}
.mask img{max-width:92vw;max-height:92vh;border-radius:24px;border:1px solid rgba(255,255,255,.15);box-shadow:0 30px 80px rgba(0,0,0,.4)}
.foot{text-align:center;color:var(--muted);font-size:12px;padding:10px 0 6px}
.sent{height:1px}
.empty{padding:24px;border:1px dashed var(--line);border-radius:24px;color:var(--muted);text-align:center;background:rgba(255,255,255,.03)}
@media (max-width: 980px){
  .hero,.layout{grid-template-columns:1fr}
  .panel{position:static}
  .search{width:100%;min-width:0;max-width:none}
}
@media (max-width: 640px){
  .shell{padding:16px 10px 28px}
  .topbar,.hero-card,.side-card,.postbox,.f-box,.panel{border-radius:22px}
  .topbar{padding:6px 4px 14px;flex-direction:column;align-items:stretch}
  .toolbar{justify-content:stretch}
  .theme-btn,.pill,.search{width:100%}
  .hero-card{padding:22px}
  .hero h2{max-width:13ch}
  .f-grid.two,.f-grid.three{grid-template-columns:1fr 1fr}
  .f-grid.two img,.f-grid.three img{height:132px}
}
@media (prefers-reduced-motion: reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{animation:none!important;transition:none!important}
  body.loaded .shell,.f-item{opacity:1;transform:none}
}
</style>
</head>
<body>
<div class="shell">
  <div class="topbar">
    <div class="brand">
      <div class="mark">Q</div>
      <div>
        <h1>Q忆</h1>
        <p>QQ archive · visual timeline</p>
      </div>
    </div>
    <div class="toolbar">
      <input id="search" class="search" type="search" placeholder="搜索文字、昵称、日期">
      <button class="theme-btn" id="themeBtn" type="button">切换明暗</button>
      <button class="theme-btn" id="paletteBtn" type="button">切换配色</button>
      <button class="pill" id="resetBtn" type="button">回到顶部</button>
    </div>
  </div>
  <section class="hero">
    <div class="hero-card">
      <div class="kicker">Memory feed</div>
      <h2>把这些片段整理成一条会呼吸的时间线。</h2>
      <p>这是你的内容年鉴：保留原帖、评论、点赞与多媒体，把旧日碎片重新排成更适合浏览和回看的样子。</p>
      <div class="hero-meta">
        <div class="hero-stat"><b id="statCount">0</b><span>总帖子</span></div>
        <div class="hero-stat"><b id="statMine">0</b><span>我的帖子</span></div>
        <div class="hero-stat"><b id="statOther">0</b><span>他人互动</span></div>
      </div>
    </div>
    <aside class="side-card">
      <div class="profile">
        <img class="p-ava" id="pava" src="" alt="">
        <div class="p-meta">
          <div class="p-name" id="pname"></div>
          <div class="p-qq" id="pqq"></div>
        </div>
      </div>
      <div class="story-row" id="storyRow"></div>
      <div class="chips" id="chips"></div>
    </aside>
  </section>
  <div class="postbox">
    <img class="b-ava" id="bava" src="" alt="">
    <div class="b-input">正在整理 2015—2026 的内容档案</div>
    <div class="actions">
      <button class="action" id="albumBtn" type="button">相</button>
      <button class="action" id="sortBtn" type="button">⟡</button>
    </div>
  </div>
  <div class="layout">
    <main class="feed" id="list"></main>
    <aside class="panel">
      <h3>Timeline</h3>
      <div class="timeline" id="timeline"></div>
    </aside>
  </div>
  <div class="sent" id="sent"></div>
  <div class="foot">数据生成于 __GEN__ · 图片视频为直链</div>
</div>
<div class="mask" id="mask" onclick="this.style.display='none'"><img id="big" src="" alt=""></div>
<script>
var POSTS = __POSTS__;
var ME = __ME__;
var state = {filter: 'all', shown: 0, step: 18, query: '', sort: 'new', album: false};
function esc(s){return String(s==null?'':s).replace(/[&<>\"']/g,function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m];});}
function fmt(t){if(!t)return '';var d=new Date(t*1000),p=function(n){return (n<10?'0':'')+n;};return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());}
function fmtLong(t){if(!t)return '';var d=new Date(t*1000);return d.getFullYear()+'年'+(d.getMonth()+1)+'月'+d.getDate()+'日';}
function cnt(p){var n=(p.comments||[]).length;for(var i=0;i<(p.comments||[]).length;i++){n+=(p.comments[i].replies||[]).length;}return n;}
function img(u,cls){if(!u)return '<span class="'+cls+' avi">?</span>';if(u.avatar)return '<img class="'+cls+'" src="'+esc(u.avatar)+'" alt="">';return '<span class="'+cls+' avi">'+esc((u.nickname||'?').charAt(0))+'</span>';}
function fmtText(s){return esc(s).replace(/\\[em\\]([0-9a-z]+)\\[\\/em\\]/gi,'<img class="emoji" src="https://qzonestyle.gtimg.cn/qzone/em/$1@2x.gif" alt="">');}
function nn(u,isMine){return '<a class="nname" href="javascript:;">'+esc(u.nickname)+'</a>'+(isMine?'<i class="tag me">我</i>':'');}
function mediaHtml(p){var h='',imgs=p.images||[];if(imgs.length){var cls=imgs.length===1?'one':imgs.length===2?'two':'three';h+='<div class="f-grid '+cls+'">';imgs.forEach(function(u){h+='<img src="'+esc(u)+'" loading="lazy" onclick="light(this.src)">';});h+='</div>';} (p.videos||[]).forEach(function(v){h+='<div class="f-video"><video src="'+esc(v.url)+'" poster="'+esc(v.cover||'')+'" controls preload="metadata"></video></div>';});return h;}
function reshareHtml(p){var r=p.reshare;if(!r)return'';return '<div class="f-share">'+img(r.author,'s-ava')+'<div class="s-bd">'+nn(r.author,String(r.author.uin)===String(ME.uin))+'<span class="colon"> : </span>'+fmtText(r.text)+'</div></div>';}
function likesHtml(p){var likes=p.likes||[],ns=[];likes.forEach(function(l){ns.push(esc(l.nickname));});(p.mentions||[]).forEach(function(m){ns.push('@'+esc(m.nickname));});if(!ns.length)return '';var tail=likes.length?'共'+likes.length+'人觉得很赞':(p.mentions.length?'提到我':'');return '<div class="f-like"><i>♥</i><span class="names">'+ns.join('、')+'</span><em>'+tail+'</em></div>';}
function commentsHtml(p){var cs=p.comments||[];if(!cs.length)return '';var h='';cs.forEach(function(c){h+='<div class="c-item">'+img(c,'c-ava')+'<div class="c-bd">'+'<div class="c-text">'+nn(c,c.is_mine)+'<span class="colon"> : </span>'+fmtText(c.content)+'</div>'+'<div class="c-meta"><span>'+fmt(c.time)+'</span><a href="javascript:;">回复</a></div>';var rs=c.replies||[];if(rs.length){h+='<div class="c-sub">';rs.forEach(function(r){var t=r.reply?r.reply:(c.nickname||'');h+='<div class="c-item">'+img(r,'c-ava')+'<div class="c-bd">'+'<div class="c-text">'+nn(r,r.is_mine)+'<i class="rep"> 回复 </i>'+esc(t)+'<span class="colon"> : </span>'+fmtText(r.content)+'</div>'+'<div class="c-meta"><span>'+fmt(r.time)+'</span><a href="javascript:;">回复</a></div>'+'</div></div>';});h+='</div>';}h+='</div></div>';});return h;}
function anchorId(p){var t=String(p.time||0);return 'y'+t.slice(0,4)+'m'+t.slice(4,6)+'_'+String(p.id||'').slice(0,8);}
function card(p){var tag=p.is_mine?'<i class="tag me">我</i>':'<i class="tag">TA</i>';var content=p.text?'<div class="f-text">'+fmtText(p.text)+'</div>':'';if(p.images.length||p.videos.length)content+=mediaHtml(p);content+=reshareHtml(p);return '<article class="f-item" id="'+anchorId(p)+'" data-year="'+(new Date(p.time*1000)).getFullYear()+'" data-has-img="'+(p.images.length?1:0)+'" data-me="'+(p.is_mine?'1':'0')+'">'+'<div class="f-box">'+'<div class="f-hd"><div class="author">'+img(p.author,'f-ava')+'<div><div>'+nn(p.author,false)+tag+'</div><div class="sub">'+fmtLong(p.time)+'</div></div></div><span class="tag">#'+esc(p.type||'post')+'</span></div>'+(content?'<div class="f-content">'+content+'</div>':'')+'<div class="f-foot"><div class="f-info"><a href="javascript:;">'+fmt(p.time)+'</a></div><div class="f-op">'+'<a href="javascript:;">↻ 转发</a>'+'<a href="javascript:;">💬 评论'+(cnt(p)?'<em>'+cnt(p)+'</em>':'')+'</a>'+'<a href="javascript:;" class="like"><i>♥</i>赞'+(p.likes.length?'<em>'+p.likes.length+'</em>':'')+'</a>'+'</div></div>'+likesHtml(p)+(p.comments.length?'<div class="f-comments">'+commentsHtml(p)+'</div>':'')+'</div></article>';
}
function chips(){var all=POSTS.length,mine=0,imgs=0;POSTS.forEach(function(p){if(p.is_mine)mine++;if((p.images||[]).length)imgs++;});document.getElementById('statCount').textContent=all;document.getElementById('statMine').textContent=mine;document.getElementById('statOther').textContent=all-mine;var el=document.getElementById('chips');el.innerHTML='<span class="chip'+(state.filter==='all'?' on':'')+'" data-f="all">全部 '+all+'</span>'+'<span class="chip'+(state.filter==='mine'?' on':'')+'" data-f="mine">我的 '+mine+'</span>'+'<span class="chip'+(state.filter==='other'?' on':'')+'" data-f="other">别人的 '+(all-mine)+'</span>'+'<span class="chip'+(state.album?' on':'')+'" data-f="album">相册 '+imgs+'</span>';}
function setF(f){state.album=f==='album'? !state.album : false;state.filter=f==='album'? 'all':f;chips();render();}
function visible(){var arr=POSTS.slice();if(state.filter!=='all')arr=arr.filter(function(p){return state.filter==='mine'?p.is_mine:!p.is_mine;});if(state.album)arr=arr.filter(function(p){return (p.images||[]).length;});if(state.query){var q=state.query.toLowerCase();arr=arr.filter(function(p){return [p.text,(p.author||{}).nickname,p.time?fmt(p.time):''].join(' ').toLowerCase().indexOf(q)>=0;});}if(state.sort==='old')arr.reverse();return arr;}
function appendMore(){var vs=visible(),list=document.getElementById('list'),s=document.getElementById('sent');var end=Math.min(state.shown+state.step,vs.length);for(;state.shown<end;state.shown++)list.insertAdjacentHTML('beforeend',card(vs[state.shown]));if(s)s.style.display=state.shown<vs.length?'':'none';}
function render(){document.getElementById('list').innerHTML='';state.shown=0;var vs=visible();if(!vs.length){document.getElementById('list').innerHTML='<div class="empty">没有找到匹配内容。</div>';document.getElementById('sent').style.display='none';return;}appendMore();updateTimeline(vs);}
function updateTimeline(vs){var tl=document.getElementById('timeline');var years={};vs.forEach(function(p){var y=(new Date(p.time*1000)).getFullYear();years[y]=(years[y]||0)+1;});var ks=Object.keys(years).sort(function(a,b){return b-a;}).slice(0,6);tl.innerHTML=ks.map(function(y){return '<button class="t-item" type="button" data-year="'+y+'"><span class="t-dot"></span><div class="t-bd"><b>'+y+' · '+years[y]+' 条</b><span>点击跳到这一年</span></div></button>';}).join('');}
function goYear(y){var el=document.querySelector('[data-year="'+y+'"]');if(el)el.scrollIntoView({behavior:'smooth',block:'start'});}
document.addEventListener('click',function(e){var ch=e.target.closest&&e.target.closest('.chip');if(ch){setF(ch.getAttribute('data-f'));e.preventDefault();return;}var t=e.target.closest&&e.target.closest('.t-item');if(t){goYear(t.getAttribute('data-year'));e.preventDefault();return;}var l=e.target.closest&&e.target.closest('.like');if(l){l.classList.toggle('on');e.preventDefault();}});
document.getElementById('albumBtn').addEventListener('click',function(){state.album=!state.album;chips();render();});
document.getElementById('themeBtn').addEventListener('click',function(){var html=document.documentElement;var next=html.getAttribute('data-theme')==='dark'?'light':'dark';html.setAttribute('data-theme',next);try{localStorage.setItem('q忆-theme',next);}catch(e){}});
document.getElementById('paletteBtn').addEventListener('click',function(){var html=document.documentElement;var names=['default','rose','aurora','mint','amber','purple'];var current=html.getAttribute('data-palette')||'default';var next=names[(names.indexOf(current)+1)%names.length];if(next==='default')html.removeAttribute('data-palette');else html.setAttribute('data-palette',next);try{localStorage.setItem('q忆-palette',next);}catch(e){}});
try{var savedPalette=localStorage.getItem('q忆-palette');if(savedPalette&&savedPalette!=='default')document.documentElement.setAttribute('data-palette',savedPalette);}catch(e){}
</script>
</body>
</html>"""




def my_profile(posts, my_uin):
    me = {"uin": str(my_uin), "nickname": "我", "avatar": avatar_url(my_uin)}
    for p in posts:
        a = p.get("author") or {}
        if str(a.get("uin") or "") == str(my_uin):
            if a.get("nickname"):
                me["nickname"] = a["nickname"]
            if a.get("avatar"):
                me["avatar"] = a["avatar"]
            break
    return me


def safe_name(s):
    s = str(s or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:80] or "unknown"


def write_viewer(posts, out, generated="", me=None):
    payload = (json.dumps(posts, ensure_ascii=False)
               .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
               .replace("</", "<\\/"))
    me_json = json.dumps(me or {}, ensure_ascii=False).replace("</", "<\\/")
    path = Path(out) / "viewer.html"
    path.write_text(VIEWER_HTML.replace("__POSTS__", payload)
                               .replace("__ME__", me_json)
                               .replace("__GEN__", generated), encoding="utf-8")
    print(f"页面 → {path}")


def main():
    conf = parse_curl(CONFIG["curl_file"])
    print(f"uin={conf['uin']}  g_tk={conf['g_tk']}")
    out = Path(CONFIG["out_dir"])
    if CONFIG["fresh"] and out.exists():
        shutil.rmtree(out)
    qz = Qzone(conf, CONFIG["request_delay"])
    feeds_dir = out / "feeds"
    if CONFIG["crawl"] and not CONFIG["pc_only"]:
        crawl_feeds(qz, feeds_dir, CONFIG["feeds_limit"])
    if CONFIG["crawl"]:
        crawl_pc(qz, out, conf["uin"], conf["g_tk"], resume=CONFIG["pc_resume"], start=CONFIG["pc_start"])
    posts = [] if CONFIG["pc_only"] else build_posts(feeds_dir, conf["uin"])
    posts = merge_pc(posts, out, conf["uin"])
    posts = filter_dead_images(posts)
    if CONFIG["fetch_my_moods"]:
        my = str(conf["uin"])
        moods = fetch_my_moods(qz, my)
        n = 0
        for p in posts:
            if str((p.get("author") or {}).get("uin")) != my:
                continue
            mood = next((m for t, m in moods.items()
                         if t[:16] == str(p.get("id") or "")[:16]), None)
            if mood:
                apply_mood(p, mood, my)
                n += 1
        print(f"自己 {n} 个帖子补了多图/时间/转发卡/评论")
    if CONFIG["save_media"]:
        download_all(posts, qz, out)
    generated = time.strftime("%Y-%m-%d %H:%M:%S")
    me = my_profile(posts, conf["uin"])
    folder = safe_name(f"{me.get('nickname') or 'QQ'}_{me.get('uin') or conf['uin']}")
    target = out / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / "data.json").write_text(json.dumps({
        "generated": generated,
        "count": len(posts),
        "posts": posts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"共 {len(posts)} 个帖子 → {target / 'data.json'}")
    write_viewer(posts, target, generated, me)


if __name__ == "__main__":
    main()
