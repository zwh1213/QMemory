import json
import re
import shutil
import threading
from pathlib import Path

from app import cookiemgr


class ProfileStore:
    def __init__(self, root):
        self.root = Path(root)
        self.output = self.root / "output"
        self.output.mkdir(parents=True, exist_ok=True)
        self.pointer = self.output / ".current_profile"
        self.lock = threading.RLock()
        self.migration_error = ""

    @staticmethod
    def dirname_for(uin):
        # 档案目录只用 QQ 号命名（网名可能有特殊字符，不参与目录名）
        return str(uin or "0")

    @staticmethod
    def uin_from_name(name):
        # 档案目录就是 QQ 号：纯数字目录才识别为档案
        name = str(name)
        return name if name.isdigit() else ""

    def _write_pointer(self, name):
        self.output.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer.with_suffix(".tmp")
        tmp.write_text(name, encoding="utf-8")
        tmp.replace(self.pointer)

    def _profile_dirs(self):
        out = []
        for p in self.output.iterdir():
            if p.is_dir() and self.uin_from_name(p.name):
                out.append(p)
        return sorted(out, key=lambda p: p.name.lower())

    def list_profiles(self):
        result = []
        current = self.active_name()
        for p in self._profile_dirs():
            cfg = cookiemgr.load(p)
            uin = self.uin_from_name(p.name) or str(cfg.get("target_uin") or cfg.get("uin") or "")
            result.append({
                "id": p.name,
                "name": p.name.rsplit("_", 1)[0] if "_" in p.name else p.name,
                "uin": uin,
                "auth_uin": str(cfg.get("auth_uin") or cfg.get("uin") or ""),
                "nickname": str(cfg.get("nickname") or (p.name.rsplit("_", 1)[0] if "_" in p.name else p.name)),
                "current": p.name == current,
                "path": str(p),
            })
        return result

    def active_name(self):
        try:
            name = self.pointer.read_text(encoding="utf-8").strip()
            if name and (self.output / name).is_dir() and self.uin_from_name(name):
                return name
        except OSError:
            pass
        dirs = self._profile_dirs()
        if dirs:
            return dirs[0].name
        return ""

    def active(self):
        name = self.active_name()
        if not name:
            return self.ensure_profile("", "未配置")
        return self.output / name

    def find_by_uin(self, uin):
        uin = str(uin or "").strip()
        if not uin:
            return None
        for p in self._profile_dirs():
            if self.uin_from_name(p.name) == uin:
                return p
            cfg = cookiemgr.load(p)
            if str(cfg.get("target_uin") or cfg.get("uin") or "") == uin:
                return p
        return None

    def ensure_profile(self, uin, nickname=""):
        uin = str(uin or "").strip()
        if uin and not re.fullmatch(r"\d+", uin):
            raise ValueError("目标 QQ 号无效")
        with self.lock:
            p = self.find_by_uin(uin) if uin else None
            if p is None:
                p = self.output / self.dirname_for(uin or "0")
                p.mkdir(parents=True, exist_ok=True)
            for name in ("datas", "imgs", "videos", "exports"):
                (p / name).mkdir(parents=True, exist_ok=True)
            if not self.active_name():
                self._write_pointer(p.name)
            return p

    def get(self, profile):
        p = self.output / str(profile)
        if not p.is_dir() or not self.uin_from_name(p.name):
            raise ValueError("账号档案不存在")
        return p

    def activate(self, profile):
        with self.lock:
            p = self.get(profile)
            self._write_pointer(p.name)
            return p

    def resolve_path(self, profile, value, default):
        base = Path(profile).resolve()
        raw = str(value or default).strip()
        path = Path(raw)
        path = path if path.is_absolute() else base / path
        return path.resolve()

    def migrate_legacy(self):
        old = self.output
        markers = [old / "config.json", old / "details.json", old / "datas", old / "imgs", old / "videos"]
        if not any(p.exists() for p in markers):
            return None
        if self._profile_dirs():
            return None
        try:
            cfg = cookiemgr.load(old)
            target = str(cfg.get("target_uin") or cfg.get("uin") or "")
            nick = str(cfg.get("nickname") or "")
            cards = old / "datas" / "pc_cards.jsonl"
            if cards.exists():
                for line in cards.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    author_uin = str(item.get("author_uin") or "")
                    if not target:
                        target = author_uin
                    if author_uin == target and item.get("author_name"):
                        nick = str(item["author_name"])
                        break
            if not target or not re.fullmatch(r"\d+", target):
                target = "0"
            p = old / self.dirname_for(target)
            if p.exists():
                raise RuntimeError("迁移目标目录已存在，未覆盖旧数据")
            p.mkdir(parents=True)
            marker = p / ".migrating"
            marker.write_text("legacy output", encoding="utf-8")
            moves = []
            for name in ("config.json", "details.json", "datas", "imgs", "videos"):
                src = old / name
                if not src.exists():
                    continue
                dst = p / ("config.json" if name == "config.json" else name)
                shutil.move(str(src), str(dst))
                moves.append((src, dst))
            new_cfg = cookiemgr.load(p)
            new_cfg["target_uin"] = target if target != "0" else ""
            new_cfg["data_dir"] = "datas"
            new_cfg["photos_dir"] = "imgs"
            new_cfg["videos_dir"] = "videos"
            new_cfg["nickname"] = nick or new_cfg.get("nickname") or "QQ"
            new_cfg["auth_uin"] = new_cfg.get("auth_uin") or new_cfg.get("uin") or ""
            cookiemgr.save(p, new_cfg)
            marker.unlink(missing_ok=True)
            self._write_pointer(p.name)
            return p
        except Exception as exc:
            self.migration_error = str(exc)
            return None
