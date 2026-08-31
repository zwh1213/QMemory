import json
import queue
import random
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from app import cookiemgr
from app.downloader import MediaDownloader
from app.qzone_spider import (BlockedError, Qzone, fetch_pc, parse_pc_card,
                              fetch_my_moods, mood_to_detail)


class CrawlEngine:
    def __init__(self, root, repository):
        self.root = Path(root)
        self.repository = repository
        cfgj = cookiemgr.load(self.root)
        # 数据目录跟随 config（采集实际写入位置），重启后据此恢复进度
        self.data_dir = self.root / (cfgj.get("data_dir") or "datas")
        self.state_path = self.data_dir / "pc_state.json"
        self.data_path = self.data_dir / "pc_cards.jsonl"
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.state = {"status": "idle", "phase": "", "offset": 0, "page": 0, "written": 0,
                      "photos": 0, "videos": 0, "error": "", "updated_at": "",
                      "target_uin": "", "download_photos": True, "download_videos": True}
        self.logs = []          # 内存环形日志（最近 200 条）
        self.log_path = self.data_dir / "crawl.log"
        self._load_state()

    def _now(self):
        return datetime.now().isoformat(timespec="seconds")

    def _log(self, text):
        line = f"[{self._now()}] {text}"
        try:
            print(line, flush=True)   # 同步输出到控制台终端
        except OSError:
            pass
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 200:
                del self.logs[:-200]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _load_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                self.state.update(state)
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self, **changes):
        with self.lock:
            self.state.update(changes, updated_at=self._now())
            payload = json.dumps(self.state, ensure_ascii=False)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.state_path)

    def is_alive(self):
        with self.lock:
            return bool(self.worker and self.worker.is_alive())

    def status(self):
        with self.lock:
            try:
                disk_state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(disk_state, dict):
                    self.state.update(disk_state)
            except (OSError, ValueError, TypeError):
                pass
            result = dict(self.state)
            result["stoppable"] = bool(self.worker and self.worker.is_alive())
            logs = list(self.logs)
            if not logs and self.log_path.exists():
                # 重启后内存日志为空，读日志文件尾部补上历史
                try:
                    logs = self.log_path.read_text(encoding="utf-8").splitlines()[-200:]
                except OSError:
                    pass
            result["logs"] = logs
            # 状态文件缺失或为 0 时，直接从已采数据推导（重启后自动恢复记忆）
            if not result.get("written"):
                try:
                    with self.data_path.open(encoding="utf-8") as f:
                        result["written"] = sum(1 for _ in f)
                except OSError:
                    pass
            if not result.get("photos") and not result.get("videos"):
                try:
                    media = json.loads((self.data_dir / "media_map.json").read_text(encoding="utf-8"))
                    result["photos"] = len(media.get("photos") or {})
                    result["videos"] = len(media.get("videos") or {})
                except (OSError, ValueError, TypeError):
                    pass
            return result

    def clear_data(self):
        """清空采集数据与已下载媒体（仅供控制台确认后调用）"""
        with self.lock:
            if self.worker and self.worker.is_alive():
                return {"ok": False, "error": "采集进行中，请先停止"}
            cfg = cookiemgr.load(self.root)
            data_dir = self.root / (cfg.get("data_dir") or "datas")
            photos_dir = self.root / (cfg.get("photos_dir") or "imgs")
            videos_dir = self.root / (cfg.get("videos_dir") or "videos")
            removed = {"files": [], "dirs": []}
            for name in ("pc_cards.jsonl", "pc_state.json", "media_map.json"):
                path = data_dir / name
                if path.exists():
                    try:
                        path.unlink()
                        removed["files"].append(name)
                    except OSError:
                        pass
            for d in (photos_dir, videos_dir):
                if d.is_dir():
                    for f in d.rglob("*"):
                        if f.is_file():
                            try:
                                f.unlink()
                            except OSError:
                                pass
                    removed["dirs"].append(d.name)
            self._save_state(status="idle", phase="", offset=0, page=0, written=0,
                             photos=0, videos=0, error="")
            self.logs.clear()
            self._log("已清空全部采集数据与媒体，下次开始采集将从头全部重采")
            self.repository.reload()
            return {"ok": True, **removed}

    def _running_elsewhere(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        if state.get("status") not in {"running", "requesting", "stopping"}:
            return False
        updated = state.get("updated_at") or ""
        try:
            age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
            return age < 120
        except (TypeError, ValueError):
            return False

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False
            if self._running_elsewhere():
                return False
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._run_pc, name="qq-pc-crawler", daemon=True)
            self.worker.start()
            return True

    def stop(self):
        with self.lock:
            active = bool(self.worker and self.worker.is_alive())
            if active:
                self.state["status"] = "stopping"
                self.state["updated_at"] = self._now()
        if active:
            self.stop_event.set()
        return active

    def _run_pc(self):
        dl = None
        workers = []
        try:
            cfg = cookiemgr.load(self.root)
            if not cfg.get("uin"):
                self._log("配置错误：未配置 Cookie，请先在控制台获取或粘贴")
                self._save_state(status="failed", phase="pc",
                                 error="未配置 Cookie，请先在控制台获取或粘贴")
                return
            token = cfg["g_tk"]
            uin = str(cfg.get("target_uin") or cfg["uin"])
            dl_photos = bool(cfg.get("download_photos", True))
            dl_videos = bool(cfg.get("download_videos", True))
            data_dir = self.root / (cfg.get("data_dir") or "datas")
            self.data_path = data_dir / "pc_cards.jsonl"
            self.state_path = data_dir / "pc_state.json"
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            state = self.status()
            offset = int(state.get("offset") or 0)
            page = int(state.get("page") or 0)
            written = int(state.get("written") or 0)
            mode = "a" if self.data_path.exists() else "w"
            qz = Qzone(cfg, delay=7.0, jitter=3.0)
            if dl_photos or dl_videos:
                # 采集线程入队，图片/视频各一个下载线程并行
                dl = MediaDownloader(self.root, cfg, qz.conf)
                n_img, n_vid = dl.enqueue_history(self.data_path)
                if n_img or n_vid:
                    self._log(f"历史媒体入队补下：图片 {n_img}，视频 {n_vid}")
                for kind in ("img", "vid"):
                    t = threading.Thread(target=self._dl_worker, args=(dl, kind), daemon=True)
                    t.start()
                    workers.append(t)
            self._save_state(status="running", phase="pc", error="", target_uin=uin,
                             download_photos=dl_photos, download_videos=dl_videos,
                             offset=offset, page=page, written=written)
            self._log(f"开始采集 QQ 空间：目标 {uin}，图片下载={dl_photos}，视频下载={dl_videos}")
            # 采集一开始就补详情，九宫格/评论采集过程中就齐全
            self._fetch_details(qz, uin)
            with self.data_path.open(mode, encoding="utf-8") as fh:
                while not self.stop_event.is_set():
                    page += 1
                    self._save_state(status="running", phase="pc", offset=offset, page=page, written=written)
                    cards, has_more, total = fetch_pc(qz, uin, token, offset)
                    if not cards:
                        self._log(f"第 {page} 页返回空，无更多内容，采集完成")
                        self._fetch_details(qz, uin)
                        self._save_state(status="completed", phase="pc", offset=offset, page=page,
                                         written=written, total=total)
                        break
                    batch = []
                    for item in cards:
                        if self.stop_event.is_set():
                            break
                        card = parse_pc_card(item, uin)
                        if not card:
                            continue
                        fh.write(json.dumps(card, ensure_ascii=False) + "\n")
                        written += 1
                        batch.append(card)
                        if dl:
                            self._enqueue_media(dl, card)
                    fh.flush()
                    self._log(f"第 {page} 页抓取 {len(cards)} 条，新增写入 {len(batch)} 条（累计 {written}）")
                    offset += len(cards)
                    counts = dl.counts_snapshot() if dl else None
                    self._save_state(status="running", phase="pc", offset=offset, page=page,
                                     written=written,
                                     photos=counts["photos"] if counts else 0,
                                     videos=counts["videos"] if counts else 0)
                    self.repository.reload()
                    if not has_more:
                        self._log(f"第 {page} 页为最后一页，共 {total} 条，采集完成")
                        self._fetch_details(qz, uin)
                        self._save_state(status="completed", phase="pc", offset=offset, page=page,
                                         written=written,
                                         photos=counts["photos"] if counts else 0,
                                         videos=counts["videos"] if counts else 0, total=total)
                        break
                    self._log(f"已抓 {page} 页，随机休眠后继续下一页")
                    self.stop_event.wait(random.uniform(2.0, 5.0))
                else:
                    self._log("收到停止信号，采集已停止")
                    self._fetch_details(qz, uin)
                    self._save_state(status="stopped", phase="pc", offset=offset, page=page,
                                     written=written)
        except BlockedError as exc:
            self._log(f"疑似风控被拦截：{exc}")
            self._save_state(status="blocked", phase="pc", error=str(exc))
        except Exception as exc:
            self._log(f"采集异常：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            self._save_state(status="failed", phase="pc",
                             error=f"{type(exc).__name__}: {exc}")
        finally:
            if dl:
                dl.finish()
                for t in workers:
                    # 停止/完成都等下载线程把队列排空再保存，最多兜底 10 分钟
                    t.join(timeout=600)
                dl.save()
                counts = dl.counts_snapshot()
                with self.lock:
                    self.state["photos"] = counts["photos"]
                    self.state["videos"] = counts["videos"]
            with self.lock:
                self.worker = None

    def _fetch_details(self, qz, uin):
        # 拉目标空间全部说说详情（完整评论/多图/时间/转发），写当前档案根目录 details.json
        try:
            moods = fetch_my_moods(qz, uin)
        except BlockedError as exc:
            self._log(f"详情补充：手机版接口疑似被封（{exc}），跳过")
            return
        except Exception as exc:
            self._log(f"详情补充跳过（手机版接口异常）：{type(exc).__name__}: {exc}")
            return
        if not moods:
            self._log("详情补充：手机版接口正常，无更多数据")
            return
        details = {}
        for tid, mo in moods.items():
            key = str(tid)
            if not key:
                continue
            try:
                details[key] = mood_to_detail(mo, uin)
            except Exception:
                continue
        dp = self.root / "details.json"
        dp.parent.mkdir(parents=True, exist_ok=True)
        tmp = dp.with_suffix(".tmp")
        tmp.write_text(json.dumps(details, ensure_ascii=False), encoding="utf-8")
        tmp.replace(dp)
        self._log(f"详情补充：mood 接口 {len(moods)} 条，写入 {dp}")
        self.repository.reload()

    def _dl_worker(self, dl, kind):
        q = dl.img_q if kind == "img" else dl.vid_q
        while True:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                # 停止时排空剩余队列再退出，把已入队媒体都下完（停止≠丢媒体）
                if self.stop_event.is_set() and q.empty():
                    break
                continue
            if item is None:
                break
            if kind == "img":
                dl.dl_photo(item)
            else:
                dl.dl_video(item)
            # 下载线程实时刷新状态计数，控制台轮询能看到进行中的进度
            counts = dl.counts_snapshot()
            with self.lock:
                if kind == "img":
                    self.state["photos"] = counts["photos"]
                else:
                    self.state["videos"] = counts["videos"]

    def _enqueue_media(self, dl, card):
        for u in card.get("images") or []:
            dl.enqueue_photo(u)
        for v in card.get("videos") or []:
            if isinstance(v, str):
                dl.enqueue_video(v)
            elif v.get("url"):
                dl.enqueue_video(v["url"])
                if v.get("cover"):
                    # 封面图下载到本地，卡片先展示封面、点击再播放视频
                    dl.enqueue_photo(v["cover"])
