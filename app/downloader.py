import hashlib
import json
import queue
import threading
from pathlib import Path

import requests

from app.qzone_spider import avatar_url, download, hd_photo_url


def _dirs(root, cfg):
    root = Path(root)
    return (root / (cfg.get("photos_dir") or "output/imgs"),
            root / (cfg.get("videos_dir") or "output/videos"))


def _new_session(conf):
    s = requests.Session()
    s.headers.update({
        "User-Agent": conf["user_agent"],
        "Referer": conf["referer"],
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    s.cookies.update(conf["cookies"])
    return s


def video_key(url):
    # 视频 URL 带 dis_k/dis_t 签名，同一视频每次抓取签名都会变；
    # 用去掉查询参数的稳定地址作为去重/映射键，避免同一视频重复下载
    return str(url).split("?")[0].strip()


# ---------- 多线程媒体下载 ----------

class MediaDownloader:
    # 采集线程入队，图片/视频各一个下载线程消费；映射与计数用锁保护
    def __init__(self, root, cfg, conf):
        self.root = Path(root)
        self.data_dir = self.root / (cfg.get("data_dir") or "output/datas")
        self.photos_dir = self.root / (cfg.get("photos_dir") or "output/imgs")
        self.videos_dir = self.root / (cfg.get("videos_dir") or "output/videos")
        self.conf = conf
        self.img_session = _new_session(conf)
        self.vid_session = _new_session(conf)
        self.img_q = queue.Queue()
        self.vid_q = queue.Queue()
        self.lock = threading.RLock()
        self.photo_map = {}
        self.video_map = {}
        self.photo_queued = set()
        self.video_queued = set()
        self.counts = {"photos": 0, "videos": 0, "photos_dead": 0,
                       "videos_dead": 0, "videos_expired": 0, "videos_fail": 0}

    def enqueue_photo(self, url):
        if not url:
            return False
        url = hd_photo_url(str(url).replace("&amp;", "&"))
        with self.lock:
            if url in self.photo_map or url in self.photo_queued:
                return False
            self.photo_queued.add(url)
        self.img_q.put(url)
        return True

    def enqueue_video(self, url):
        if not url:
            return False
        url = str(url).replace("\\/", "/")
        key = video_key(url)
        with self.lock:
            if key in self.video_map or key in self.video_queued:
                return False
            self.video_queued.add(key)
        self.vid_q.put(url)
        return True

    def dl_photo(self, url):
        if not url:
            return ""
        url = hd_photo_url(str(url).replace("&amp;", "&"))
        if url in self.photo_map:
            return self.photo_map[url]
        if "qzone/app/video/res/404_16x9.png" in url:
            # 视频已失效占位图，跳过不下载
            with self.lock:
                self.photo_map[url] = ""
                self.counts["photos_dead"] += 1
                self._maybe_save()
            return ""
        name = hashlib.md5(url.encode()).hexdigest()
        fn = download(self.img_session, url, self.photos_dir, name,
                      self.conf["referer"], "image")
        with self.lock:
            self.photo_map[url] = f"/media/photos/{fn}" if fn else ""
            if fn:
                self.counts["photos"] += 1
            self._maybe_save()
        return self.photo_map[url]

    def dl_video(self, url):
        if not url:
            return ""
        url = str(url).replace("\\/", "/")
        key = video_key(url)
        if key in self.video_map:
            return self.video_map[key]
        if "qzone/app/video/res/404_16x9.mp4" in url:
            # 视频已失效（QQ 空间占位），跳过不下载
            with self.lock:
                self.video_map[key] = ""
                self.counts["videos_dead"] += 1
                self._maybe_save()
            return ""
        name = hashlib.md5(key.encode()).hexdigest()
        fn = download(self.vid_session, url, self.videos_dir, name,
                      self.conf["referer"], "video")
        with self.lock:
            self.video_map[key] = f"/media/videos/{fn}" if fn else ""
            if fn:
                self.counts["videos"] += 1
            elif "photovideo.photo.qq.com" in url:
                # 播放链接带 dis_k/dis_t 签名，过期后 CDN 返回 403，无法绕过
                self.counts["videos_expired"] += 1
            else:
                self.counts["videos_fail"] += 1
            self._maybe_save()
        return self.video_map[key]

    def enqueue_history(self, path):
        # 扫描已有 pc_cards.jsonl，把没下过的媒体入队（续采时补下载历史）
        n_img = n_vid = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                card = json.loads(line)
                for u in card.get("images") or []:
                    if self.enqueue_photo(u):
                        n_img += 1
                for v in card.get("videos") or []:
                    if isinstance(v, str):
                        if self.enqueue_video(v):
                            n_vid += 1
                    elif v.get("url"):
                        if self.enqueue_video(v["url"]):
                            n_vid += 1
                        if v.get("cover") and self.enqueue_photo(v["cover"]):
                            n_img += 1
        except (OSError, ValueError, TypeError):
            pass
        return n_img, n_vid

    def _maybe_save(self):
        # 每下成功/跳过(失效)一个媒体就落盘一次 media_map，
        # 让采集进行中 viewer 也能实时看到已下好的本地媒体
        self.save()

    def save(self):
        mapping = {"photos": dict(self.photo_map), "videos": dict(self.video_map),
                   "avatars": {}}
        path = self.data_dir / "media_map.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def finish(self):
        # 放两个哨兵，通知图片/视频下载线程把队列排空后退出
        self.img_q.put(None)
        self.vid_q.put(None)

    def counts_snapshot(self):
        with self.lock:
            return dict(self.counts)


# ---------- 单线程兼容（旧引擎流程用） ----------

def download_media(qz, root, cfg, posts, download_photos=True, download_videos=True, download_avatars=False):
    photos_dir, videos_dir = _dirs(root, cfg)
    avatars_dir = photos_dir / "avatars"
    photo_map, video_map, avatar_map = {}, {}, {}
    counts = {"photos": 0, "videos": 0, "avatars": 0, "photos_dead": 0,
              "videos_dead": 0, "videos_expired": 0, "videos_fail": 0}

    def dl_photo(url):
        if not url:
            return ""
        url = hd_photo_url(str(url).replace("&amp;", "&"))
        if url in photo_map:
            return photo_map[url]
        if "qzone/app/video/res/404_16x9.png" in url:
            photo_map[url] = ""
            counts["photos_dead"] += 1
            return ""
        name = hashlib.md5(url.encode()).hexdigest()
        fn = download(qz.s, url, photos_dir, name, qz.conf["referer"], "image")
        photo_map[url] = f"/media/photos/{fn}" if fn else ""
        if fn:
            counts["photos"] += 1
        return photo_map[url]

    def dl_video(url):
        if not url:
            return ""
        url = str(url).replace("\\/", "/")
        if url in video_map:
            return video_map[url]
        if "qzone/app/video/res/404_16x9.mp4" in url:
            video_map[url] = ""
            counts["videos_dead"] += 1
            return ""
        name = hashlib.md5(url.encode()).hexdigest()
        fn = download(qz.s, url, videos_dir, name, qz.conf["referer"], "video")
        video_map[url] = f"/media/videos/{fn}" if fn else ""
        if fn:
            counts["videos"] += 1
        elif "photovideo.photo.qq.com" in url:
            counts["videos_expired"] += 1
        else:
            counts["videos_fail"] += 1
        return video_map[url]

    def dl_avatar(uin):
        if not uin:
            return ""
        key = str(uin)
        if key in avatar_map:
            return avatar_map[key]
        fn = download(qz.s, avatar_url(uin), avatars_dir, key,
                      "https://user.qzone.qq.com/", "image")
        avatar_map[key] = f"/media/avatars/{fn}" if fn else ""
        if fn:
            counts["avatars"] += 1
        return avatar_map[key]

    for post in posts:
        if download_avatars:
            author = post.get("author") or {}
            if author.get("uin"):
                author["avatar"] = dl_avatar(author["uin"])
        if download_photos:
            images = []
            for u in post.get("images") or []:
                p = dl_photo(u)
                if p:
                    images.append(p)
            post["images"] = images
        if download_videos:
            for v in post.get("videos") or []:
                if isinstance(v, str):
                    dl_video(v)
                else:
                    if v.get("url"):
                        v["path"] = dl_video(v["url"])

    mapping = {"photos": photo_map, "videos": video_map, "avatars": avatar_map}
    return mapping, counts


def save_mapping(save_root, mapping):
    path = Path(save_root) / "media_map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return path


def load_mapping(save_root):
    path = Path(save_root) / "media_map.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"photos": {}, "videos": {}, "avatars": {}}
