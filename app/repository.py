import html
import json
import hashlib
import re
import threading
from datetime import datetime
from pathlib import Path

from app import cookiemgr
from app.downloader import video_key


class ArchiveRepository:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.posts = []
        self.generated = ""
        self.dataset_id = ""
        self.reload()

    def _load_json(self, path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def reload(self):
        cfgj = cookiemgr.load(self.root)
        my_uin = str(cfgj.get("target_uin") or cfgj.get("uin") or "")
        data = self._load_json(self.root / "data.json")
        posts = (data or {}).get("posts") if isinstance(data, dict) else []
        posts = posts if isinstance(posts, list) else []
        # PC 卡片按 tid 归并成帖子（对齐 QQ 版 merge_pc）。
        pc_bases = [cfgj.get("data_dir") or "datas"]
        by_tid = {}
        for base in pc_bases:
            path = self.root / base / "pc_cards.jsonl"
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    raw = json.loads(line)
                    tid = str(raw.get("tid") or "")
                    if not tid:
                        continue
                    d = by_tid.setdefault(tid, {"cards": [], "min": 10 ** 18})
                    d["cards"].append(raw)
                    if raw.get("abstime"):
                        d["min"] = min(d["min"], raw["abstime"])
            except (OSError, ValueError, TypeError):
                continue
        existing = {str(p.get("id") or ""): p for p in posts}
        for tid, d in by_tid.items():
            cards = d["cards"]
            likes, comments, images, videos, text = [], [], [], [], ""
            for c in cards:
                if c.get("images") and not images:
                    images = [html.unescape(v) for v in c["images"]]
                if c.get("videos") and not videos:
                    videos = []
                    for _v in c.get("videos") or []:
                        if isinstance(_v, str):
                            videos.append({"url": html.unescape(_v), "cover": "", "path": ""})
                        elif isinstance(_v, dict) and _v.get("url"):
                            videos.append({"url": html.unescape(str(_v["url"])),
                                           "cover": html.unescape(str(_v.get("cover") or "")), "path": ""})
                if c.get("text") and not text:
                    text = html.unescape(c["text"])
                if c.get("feedstype") == "101" or "赞" in (c.get("action") or ""):
                    if c.get("actor_uin"):
                        likes.append({"uin": str(c["actor_uin"]),
                                      "nickname": c.get("actor_nick") or "", "avatar": ""})
                elif c.get("comment"):
                    cc = c["comment"]
                    replies = [{"uin": str(r.get("uin") or ""), "nickname": r.get("nickname") or "",
                                "avatar": "", "content": r.get("content") or "",
                                "time": r.get("time") or cc.get("time") or 0,
                                "is_mine": str(r.get("uin")) == my_uin}
                               for r in cc.get("replies") or []]
                    comments.append({"uin": str(cc.get("uin") or ""),
                                     "nickname": cc.get("nickname") or "", "avatar": "",
                                     "content": cc.get("content") or "",
                                     "time": cc.get("time") or c.get("abstime") or 0,
                                     "is_mine": str(cc.get("uin")) == my_uin,
                                     "replies": replies})
            likes = list({(x["uin"], x["nickname"]): x for x in likes}.values())
            comments = list({(x["uin"], x["nickname"], x["content"]): x for x in comments}.values())
            first = cards[0]
            post = {"id": tid, "type": "pc",
                    "time": d["min"] if d["min"] < 10 ** 18 else first.get("abstime") or 0,
                    "author": {"uin": first.get("author_uin") or "", "nickname": first.get("author_name") or "", "avatar": ""},
                    "is_mine": str(first.get("author_uin")) == my_uin,
                    "text": text, "images": images, "videos": videos,
                    "comments": comments, "likes": likes, "mentions": []}
            if tid in existing:
                old = existing[tid]
                old["likes"] = list({(x["uin"], x["nickname"]): x
                                     for x in (old.get("likes") or []) + likes}.values())
                old["comments"] = list({(x["uin"], x["nickname"], x["content"]): x
                                        for x in (old.get("comments") or []) + comments}.values())
                if not old.get("images") and images:
                    old["images"] = images
                if not old.get("videos") and videos:
                    old["videos"] = videos
                if not old.get("text") and text:
                    old["text"] = text
            else:
                posts.append(post)
        photo_map, video_map = {}, {}
        for folder in (cfgj.get("data_dir", "datas"),):
            try:
                media = json.loads((self.root / folder / "media_map.json").read_text(encoding="utf-8"))
                photo_map = media.get("photos") or {}
                video_map = media.get("videos") or {}
                if photo_map or video_map:
                    break
            except (OSError, ValueError, TypeError):
                continue
        normalized = []
        seen = set()
        for post in posts:
            if not isinstance(post, dict):
                continue
            item = dict(post)
            item["images"] = [html.unescape(str(url)).replace("\\/", "/") for url in item.get("images") or []]
            item["text"] = item.get("text") or ""
            videos = []
            for video in item.get("videos") or []:
                if isinstance(video, str):
                    videos.append({"url": video, "cover": "", "path": ""})
                elif isinstance(video, dict) and video.get("url"):
                    videos.append({"url": html.unescape(str(video["url"])).replace("\\/", "/"), "cover": html.unescape(str(video.get("cover", ""))).replace("\\/", "/"), "path": video.get("path", "")})
            item["videos"] = videos
            key = str(item.get("id") or "")
            if not key:
                key = hashlib.md5(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        details = {}
        dp = self.root / "details.json"
        if dp.exists():
            try:
                details = json.loads(dp.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                details = {}
        for item in normalized:
            d = details.get(str(item.get("id") or ""))
            if not d:
                continue
            if d.get("images"):
                item["images"] = [html.unescape(str(u)).replace("\\/", "/") for u in d["images"]]
            if d.get("videos"):
                videos = []
                for v in d["videos"]:
                    if isinstance(v, str):
                        videos.append({"url": html.unescape(v).replace("\\/", "/"), "cover": "", "path": ""})
                    elif isinstance(v, dict) and v.get("url"):
                        videos.append({"url": html.unescape(str(v["url"])).replace("\\/", "/"),
                                       "cover": html.unescape(str(v.get("cover") or "")).replace("\\/", "/"),
                                       "path": v.get("path", "")})
                if videos:
                    item["videos"] = videos
            if d.get("comments"):
                item["comments"] = self._clean_detail_comments(d["comments"])
            if d.get("time"):
                try:
                    item["time"] = int(d["time"])
                except (TypeError, ValueError):
                    pass
            if d.get("text"):
                item["text"] = d["text"]
            if d.get("reshare"):
                item["reshare"] = d["reshare"]
        for item in normalized:
            if photo_map:
                item["images"] = [photo_map.get(u, u) for u in item.get("images") or []]
            for v in item.get("videos") or []:
                if isinstance(v, dict) and v.get("url") and not v.get("path") and video_map.get(video_key(v["url"])):
                    v["path"] = video_map[video_key(v["url"])]
                if isinstance(v, dict) and v.get("cover") and photo_map.get(v["cover"]):
                    v["cover"] = photo_map[v["cover"]]
        normalized.sort(key=lambda p: int(p.get("time") or 0), reverse=True)
        with self.lock:
            self.posts = normalized
            self.generated = (data or {}).get("generated", "") if isinstance(data, dict) else ""
            stamp = json.dumps([(p.get("id"), p.get("time")) for p in normalized], ensure_ascii=False).encode()
            self.dataset_id = hashlib.sha1(stamp).hexdigest()[:16]

    def snapshot(self):
        with self.lock:
            return self.dataset_id, list(self.posts)

    def query(self, page=1, page_size=50, query="", owner="all", album=False,
              year=None, month=None, start_date="", end_date="", sort="new", snapshot=""):
        with self.lock:
            if snapshot and snapshot != self.dataset_id:
                return None
            posts = list(self.posts)
            dataset_id = self.dataset_id
        query = (query or "").strip().lower()
        if owner == "mine":
            posts = [p for p in posts if p.get("is_mine")]
        elif owner == "other":
            posts = [p for p in posts if not p.get("is_mine")]
        if album:
            posts = [p for p in posts if p.get("images")]
        if query:
            posts = [p for p in posts if query in " ".join([
                str(p.get("text") or ""),
                str((p.get("author") or {}).get("nickname") or ""),
                self.date_text(p.get("time")),
            ]).lower()]
        if year:
            posts = [p for p in posts if self.date_parts(p.get("time"))[0] == int(year)]
        if month:
            posts = [p for p in posts if self.date_parts(p.get("time"))[1] == int(month)]
        if start_date:
            posts = [p for p in posts if self.date_text(p.get("time")) >= start_date]
        if end_date:
            posts = [p for p in posts if self.date_text(p.get("time")) <= end_date]
        if sort == "old":
            posts.reverse()
        total = len(posts)
        start = (page - 1) * page_size
        items = posts[start:start + page_size]
        return {"items": items, "page": page, "page_size": page_size,
                "total": total, "has_more": start + len(items) < total,
                "dataset_id": dataset_id}

    @staticmethod
    def _clean_at(text):
        text = re.sub(r"@\{uin:\d+,nick:([^,}]+)(?:,[^}]*)?\}", r"@\1", text)
        text = re.sub(r"@\{nick:([^,}]+)(?:,[^}]*)?\}", r"@\1", text)
        return text

    def _clean_detail_comments(self, comments):
        out = []
        for c in comments or []:
            item = dict(c)
            item["content"] = self._clean_at(str(item.get("content") or ""))
            item["replies"] = []
            for r in c.get("replies") or []:
                rr = dict(r)
                rr["content"] = self._clean_at(str(rr.get("content") or ""))
                item["replies"].append(rr)
            out.append(item)
        return out

    @staticmethod
    def date_parts(timestamp):
        try:
            dt = datetime.fromtimestamp(int(timestamp))
            return dt.year, dt.month
        except (TypeError, ValueError, OSError, OverflowError):
            return 0, 0

    @classmethod
    def date_text(cls, timestamp):
        try:
            return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    def archive(self):
        years = {}
        months = {}
        with self.lock:
            posts = list(self.posts)
        for post in posts:
            year, month = self.date_parts(post.get("time"))
            if not year:
                continue
            years[str(year)] = years.get(str(year), 0) + 1
            key = f"{year:04d}-{month:02d}"
            months[key] = months.get(key, 0) + 1
        return {"years": years, "months": months, "count": len(posts), "dataset_id": self.dataset_id}
