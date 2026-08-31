import threading
from pathlib import Path

from app.engine import CrawlEngine
from app.profiles import ProfileStore
from app.repository import ArchiveRepository


class AccountManager:
    def __init__(self, root):
        self.root = Path(root)
        self.profiles = ProfileStore(self.root)
        self.profiles.migrate_legacy()
        self.lock = threading.RLock()
        self.contexts = {}

    def _id(self, profile):
        return Path(profile).name

    def context(self, profile=None, activate=False):
        if profile is None:
            path = self.profiles.active()
        elif activate:
            path = self.profiles.activate(profile)
        else:
            path = self.profiles.get(profile)
        key = self._id(path)
        with self.lock:
            ctx = self.contexts.get(key)
            if ctx is None:
                repository = ArchiveRepository(path)
                engine = CrawlEngine(path, repository)
                ctx = {"id": key, "root": path, "repository": repository, "engine": engine}
                self.contexts[key] = ctx
            return ctx

    def create_or_update(self, uin, nickname="", activate=True):
        path = self.profiles.ensure_profile(uin, nickname)
        if activate:
            self.profiles.activate(path.name)
        return self.context(path.name)

    def select(self, profile):
        return self.context(profile, activate=True)

    def current(self):
        return self.context()

    def list_profiles(self):
        rows = self.profiles.list_profiles()
        with self.lock:
            for row in rows:
                ctx = self.contexts.get(row["id"])
                if ctx:
                    row["status"] = ctx["engine"].status().get("status")
                else:
                    row["status"] = "idle"
        return rows

    def any_worker_alive(self):
        with self.lock:
            return any(ctx["engine"].is_alive() for ctx in self.contexts.values())

    def running_profiles(self):
        with self.lock:
            return [{"id": key, "status": ctx["engine"].status().get("status")}
                    for key, ctx in self.contexts.items() if ctx["engine"].is_alive()]
