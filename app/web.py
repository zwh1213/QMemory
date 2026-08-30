import sys
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_file

from app import cookiemgr
from app.qlogin import QrLogin
from app.qzone_spider import avatar_url, fetch_nickname


def _int_arg(name, default, minimum, maximum):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates" if (BASE_DIR / "templates").is_dir() else BASE_DIR.parent / "templates"


def create_app(repository, engine):
    app = Flask(__name__)
    qr_login = QrLogin(engine.root)

    @app.get("/api/cookie/qr")
    def cookie_qr():
        try:
            info = qr_login.create()
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 502
        return jsonify(ok=True, qr_id=info["qr_id"], image=info["image"])

    @app.get("/api/cookie/qr/<qr_id>")
    def cookie_qr_poll(qr_id):
        return jsonify(qr_login.poll(qr_id))

    @app.get("/")
    def index():
        return send_file(TEMPLATES_DIR / "viewer.html")

    @app.get("/favicon.ico")
    def favicon():
        # exe 内置图标优先，源码运行用项目根 imgs
        cands = []
        if getattr(sys, "frozen", False):
            cands.append(Path(sys._MEIPASS) / "imgs" / "icon.ico")
        cands.append(BASE_DIR.parent / "imgs" / "icon.ico")
        for p in cands:
            if p.is_file():
                return send_file(p, mimetype="image/x-icon")
        return "", 404

    @app.get("/console")
    def console():
        return send_file(TEMPLATES_DIR / "console.html")

    @app.get("/videos/<path:name>")
    def video(name):
        path = Path(engine.root) / "videos" / name
        if not path.exists() or path.is_dir():
            return jsonify(error="视频不存在"), 404
        return send_file(path, conditional=True)

    @app.get("/media/<kind>/<path:name>")
    def media(kind, name):
        if kind not in {"photos", "videos", "avatars"}:
            return jsonify(error="类型无效"), 400
        try:
            cfg = cookiemgr.load(engine.root)
        except ValueError:
            cfg = {}
        if kind == "videos":
            base = Path(engine.root) / (cfg.get("videos_dir") or "output/videos")
        else:
            base = Path(engine.root) / (cfg.get("photos_dir") or "output/imgs")
            if kind == "avatars":
                base = base / "avatars"
        path = base / name
        if not path.exists() or path.is_dir():
            return jsonify(error="文件不存在"), 404
        return send_file(path, conditional=True)

    @app.get("/api/data")
    def data():
        repository.reload()
        dataset_id, posts = repository.snapshot()
        cfg = cookiemgr.load(engine.root)
        my_uin = str(cfg.get("uin") or "")
        # 档案主人优先取采集目标 QQ 号，其次登录者；昵称从配置或采集数据兜底
        me_uin = str(cfg.get("target_uin") or my_uin)
        avatar = avatar_url(me_uin) if me_uin else ""
        nickname = fetch_nickname(cfg, me_uin)
        me = {"uin": me_uin, "nickname": nickname or me_uin, "avatar": avatar}
        if not nickname:
            # 兜底：从采集数据里找该 uin 的作者昵称/头像
            fallback = ""
            for post in posts:
                author = post.get("author") or {}
                anick = author.get("nickname") or ""
                if anick:
                    if not fallback:
                        fallback = anick
                    if str(author.get("uin") or "") == me_uin:
                        me["nickname"] = anick
                        if author.get("avatar"):
                            me["avatar"] = author["avatar"]
                        break
            else:
                if fallback:
                    me["nickname"] = fallback
        return jsonify({"generated": repository.generated, "count": len(posts),
                        "posts": posts, "me": me, "dataset_id": dataset_id})

    @app.get("/api/posts")
    def posts():
        page = _int_arg("page", 1, 1, 1000000)
        page_size = _int_arg("page_size", 50, 1, 50)
        year = request.args.get("year") or None
        month = request.args.get("month") or None
        for value, low, high in ((year, 1, 9999), (month, 1, 12)):
            if value is not None:
                try:
                    if not low <= int(value) <= high:
                        return jsonify(error="日期参数无效"), 400
                except ValueError:
                    return jsonify(error="日期参数无效"), 400
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        for value in (start_date, end_date):
            if value:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    return jsonify(error="日期格式应为 YYYY-MM-DD"), 400
        if start_date and end_date and start_date > end_date:
            return jsonify(error="开始日期不能晚于结束日期"), 400
        owner = request.args.get("owner", "all")
        if owner not in {"all", "mine", "other"}:
            return jsonify(error="owner 参数无效"), 400
        sort = request.args.get("sort", "new")
        if sort not in {"new", "old"}:
            return jsonify(error="sort 参数无效"), 400
        result = repository.query(
            page=page, page_size=page_size, query=request.args.get("query", ""),
            owner=owner, album=request.args.get("album") == "1",
            year=year, month=month, start_date=start_date, end_date=end_date,
            sort=sort, snapshot=request.args.get("snapshot", ""),
        )
        if result is None:
            return jsonify(error="数据已更新，请重新加载", code="DATASET_CHANGED"), 409
        return jsonify(result)

    @app.get("/api/archive")
    def archive():
        return jsonify(repository.archive())

    # 心跳：控制台页面每 1.5s 轮询 /api/jobs/status，作为"页面还开着"的信号
    last_beat = {"t": time.time()}

    @app.get("/api/jobs/status")
    def job_status():
        last_beat["t"] = time.time()
        return jsonify(engine.status())

    @app.get("/api/logs")
    def get_logs():
        # 返回 crawl.log 完整内容（日志弹窗用），文件不存在时返回空
        try:
            text = engine.log_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        return jsonify(ok=True, text=text)

    @app.post("/api/jobs/start")
    def start():
        return jsonify(started=engine.start(), status=engine.status())

    @app.post("/api/jobs/stop")
    def stop():
        return jsonify(stopped=engine.stop(), status=engine.status())

    @app.post("/api/data/clear")
    def data_clear():
        return jsonify(engine.clear_data())

    @app.post("/api/config/cookie")
    def update_cookie():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text", "")).strip()
        target_uin = str(payload.get("target_uin", "")).strip()
        try:
            conf = cookiemgr.apply_cookie(engine.root, text, target_uin)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, uin=conf["uin"], g_tk=conf["g_tk"])

    @app.get("/api/config/status")
    def config_status():
        try:
            cfg = cookiemgr.load(engine.root)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc))
        if not cfg.get("uin"):
            return jsonify(ok=False, error="未配置 Cookie，请先自动获取或手动粘贴",
                           uin="", g_tk=0,
                           photos_dir=cfg.get("photos_dir") or "output/imgs",
                           videos_dir=cfg.get("videos_dir") or "output/videos",
                           data_dir=cfg.get("data_dir") or "output/datas",
                           target_uin="",
                           download_photos=bool(cfg.get("download_photos", True)),
                           download_videos=bool(cfg.get("download_videos", True)),
                           source=cfg.get("source", ""))
        me_uin = str(cfg.get("target_uin") or cfg["uin"])
        return jsonify(ok=True, uin=cfg["uin"], g_tk=cfg["g_tk"],
                       nickname=fetch_nickname(cfg, me_uin) or me_uin,
                       avatar=avatar_url(me_uin) if me_uin else "",
                       photos_dir=cfg.get("photos_dir") or "output/imgs",
                       videos_dir=cfg.get("videos_dir") or "output/videos",
                       data_dir=cfg.get("data_dir") or "output/datas",
                       target_uin=cfg.get("target_uin") or cfg["uin"],
                       download_photos=bool(cfg.get("download_photos", True)),
                       download_videos=bool(cfg.get("download_videos", True)),
                       source=cfg.get("source", ""))

    @app.post("/api/config/settings")
    def update_settings():
        payload = request.get_json(silent=True) or {}
        cfg = cookiemgr.load(engine.root)
        for key in ("photos_dir", "videos_dir", "data_dir"):
            if key in payload and str(payload.get(key, "")).strip():
                cfg[key] = str(payload[key]).strip()
        if "download_photos" in payload:
            cfg["download_photos"] = bool(payload["download_photos"])
        if "download_videos" in payload:
            cfg["download_videos"] = bool(payload["download_videos"])
        if "target_uin" in payload and str(payload.get("target_uin", "")).strip():
            cfg["target_uin"] = str(payload["target_uin"]).strip()
        cookiemgr.save(engine.root, cfg)
        return jsonify(ok=True)

    # 心跳供 main 主循环判定"页面已关闭"，实现关 tab 自动退出
    app.last_beat = last_beat

    return app
