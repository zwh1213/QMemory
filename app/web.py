import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from app import cookiemgr
from app.qlogin import QrLogin
from app.qzone_spider import avatar_url, fetch_nickname


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates" if (BASE_DIR / "templates").is_dir() else BASE_DIR.parent / "templates"


def _inside(base, path):
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE_DIR.parent


def _full_rel(root, value):
    # 显示用：相对程序目录的完整相对路径，如 output/昵称_QQ/imgs
    try:
        return str((root.relative_to(APP_ROOT) / value).as_posix())
    except ValueError:
        return str(value)


def _strip_root(root, value):
    # 保存用：把表单里的完整相对路径剥回档案目录内的相对值
    try:
        prefix = str(root.relative_to(APP_ROOT).as_posix())
    except ValueError:
        return str(value)
    if value == prefix:
        return "."
    if value.startswith(prefix + "/"):
        return value[len(prefix) + 1:]
    return str(value)


def create_app(manager):
    app = Flask(__name__)
    qr_login = QrLogin(manager.root)
    app.last_beat = {"t": time.monotonic()}

    def ctx(profile=None):
        try:
            return manager.context(profile)
        except ValueError as exc:
            return None, (jsonify(error=str(exc)), 404)

    def profile_from_request():
        return request.args.get("profile") or (request.get_json(silent=True) or {}).get("profile") or None

    def active_ctx():
        result = ctx(profile_from_request())
        return result if isinstance(result, tuple) else (result, None)

    def status_payload(context):
        data = context["engine"].status()
        data["profile_id"] = context["id"]
        data["running_profiles"] = manager.running_profiles()
        return data

    @app.get("/")
    def index():
        return send_file(TEMPLATES_DIR / "viewer.html")

    @app.get("/console")
    def console():
        return send_file(TEMPLATES_DIR / "console.html")

    @app.get("/favicon.ico")
    def favicon():
        cands = []
        if getattr(sys, "frozen", False):
            cands.append(Path(sys._MEIPASS) / "imgs" / "icon.ico")
        cands.append(BASE_DIR.parent / "imgs" / "icon.ico")
        for p in cands:
            if p.is_file():
                return send_file(p, mimetype="image/x-icon")
        return "", 404

    @app.post("/api/heartbeat")
    def heartbeat():
        app.last_beat["t"] = time.monotonic()
        return jsonify(ok=True)

    @app.post("/api/shutdown")
    def shutdown():
        # 控制台「退出程序」：主循环检测到标志后干净退出
        app.should_stop = True
        return jsonify(ok=True)

    @app.get("/api/profiles")
    def profiles():
        return jsonify(ok=True, current=manager.profiles.active_name(), profiles=manager.list_profiles(),
                       migration_error=manager.profiles.migration_error)

    @app.post("/api/profiles/select")
    def select_profile():
        payload = request.get_json(silent=True) or {}
        try:
            context = manager.select(payload.get("profile", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(ok=True, profile=context["id"], status=status_payload(context))

    @app.get("/api/data")
    def data():
        context, error = active_ctx()
        if error:
            return error
        repository = context["repository"]
        repository.reload()
        dataset_id, posts = repository.snapshot()
        cfg = cookiemgr.load(context["root"])
        me_uin = str(cfg.get("target_uin") or cfg.get("uin") or "")
        nickname = fetch_nickname(cfg, me_uin) or cfg.get("nickname") or me_uin
        return jsonify(generated=repository.generated, count=len(posts), posts=posts,
                       me={"uin": me_uin, "nickname": nickname, "avatar": avatar_url(me_uin) if me_uin else ""},
                       dataset_id=dataset_id, profile_id=context["id"])

    @app.get("/api/archive")
    def archive():
        context, error = active_ctx()
        if error:
            return error
        return jsonify(context["repository"].archive())

    @app.get("/api/jobs/status")
    def job_status():
        app.last_beat["t"] = time.monotonic()
        context, error = active_ctx()
        if error:
            return error
        return jsonify(status_payload(context))

    @app.get("/api/logs")
    def logs():
        context, error = active_ctx()
        if error:
            return error
        try:
            text = context["engine"].log_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        return jsonify(ok=True, text=text)

    @app.post("/api/jobs/start")
    def start():
        context, error = active_ctx()
        if error:
            return error
        return jsonify(started=context["engine"].start(), status=status_payload(context))

    @app.post("/api/jobs/stop")
    def stop():
        context, error = active_ctx()
        if error:
            return error
        return jsonify(stopped=context["engine"].stop(), status=status_payload(context))

    @app.post("/api/data/clear")
    def clear_data():
        context, error = active_ctx()
        if error:
            return error
        return jsonify(context["engine"].clear_data())

    @app.post("/api/profiles/target")
    def profiles_target():
        # 用当前档案的 Cookie 为目标 QQ 创建/激活独立档案
        context, error = active_ctx()
        if error:
            return error
        payload = request.get_json(silent=True) or {}
        target_uin = str(payload.get("target_uin") or "").strip()
        if not target_uin.isdigit():
            return jsonify(error="目标 QQ 号无效"), 400
        cfg = cookiemgr.load(context["root"])
        if not cfg.get("cookies"):
            return jsonify(error="当前档案未配置 Cookie，请先登录或粘贴"), 400
        nickname = fetch_nickname(cfg, target_uin) or f"QQ_{target_uin}"
        new_ctx = manager.create_or_update(target_uin, nickname, activate=True)
        if new_ctx["engine"].is_alive():
            return jsonify(error="该账号正在采集，不能替换 Cookie"), 409
        new_cfg = cookiemgr.load(new_ctx["root"])
        new_cfg.update({"cookies": cfg.get("cookies"), "uin": cfg.get("uin"),
                        "auth_uin": cfg.get("auth_uin") or cfg.get("uin", ""),
                        "g_tk": cfg.get("g_tk"), "user_agent": cfg.get("user_agent"),
                        "referer": cfg.get("referer"), "source": "inherit",
                        "target_uin": target_uin, "nickname": nickname})
        cookiemgr.save(new_ctx["root"], new_cfg)
        new_ctx["repository"].reload()
        return jsonify(ok=True, profile=new_ctx["id"], nickname=nickname,
                       uin=target_uin, avatar=avatar_url(target_uin))

    def save_conf(conf, nickname, target_uin, source):
        target_uin = str(target_uin or conf["uin"]).strip()
        if not target_uin.isdigit():
            raise ValueError("目标 QQ 号无效")
        if str(target_uin) != str(conf["uin"]):
            # 目标是别的号：用目标资料识别昵称，别把登录号昵称串过去
            probe = {**conf, "auth_uin": str(conf["uin"])}
            nickname = fetch_nickname(probe, target_uin) or ""
        context = manager.create_or_update(target_uin, nickname or f"QQ_{target_uin}", activate=True)
        if context["engine"].is_alive():
            raise ValueError("该账号正在采集，不能替换 Cookie")
        cfg = cookiemgr.load(context["root"])
        cfg.update({"cookies": conf["cookies"], "uin": conf["uin"], "auth_uin": conf["uin"],
                    "g_tk": conf["g_tk"], "user_agent": conf["user_agent"], "referer": conf["referer"],
                    "source": source, "target_uin": target_uin})
        if nickname:
            cfg["nickname"] = nickname
        cookiemgr.save(context["root"], cfg)
        context["repository"].reload()
        return context

    @app.post("/api/config/cookie")
    def cookie():
        payload = request.get_json(silent=True) or {}
        try:
            conf = cookiemgr.parse_cookie_text(str(payload.get("text", "")))
            context = save_conf(conf, "", payload.get("target_uin", ""), "manual")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, uin=conf["uin"], g_tk=conf["g_tk"], profile=context["id"])

    @app.get("/api/cookie/qr")
    def cookie_qr():
        try:
            info = qr_login.create()
            return jsonify(ok=True, qr_id=info["qr_id"], image=info["image"])
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 502

    @app.get("/api/cookie/qr/<qr_id>")
    def cookie_qr_poll(qr_id):
        result = qr_login.poll(qr_id)
        if result.get("state") != "ok":
            return jsonify(result)
        try:
            context = save_conf(result.pop("conf"), result.get("nickname", ""), request.args.get("target_uin", ""), "qr")
            result["profile"] = context["id"]
        except ValueError as exc:
            return jsonify(state="error", error=str(exc))
        return jsonify(result)

    @app.get("/api/config/status")
    def config_status():
        context, error = active_ctx()
        if error:
            return error
        cfg = cookiemgr.load(context["root"])
        target = str(cfg.get("target_uin") or cfg.get("uin") or "")
        return jsonify(ok=bool(cfg.get("uin")), uin=cfg.get("uin", ""), auth_uin=cfg.get("auth_uin") or cfg.get("uin", ""),
                       g_tk=cfg.get("g_tk", 0), target_uin=target, nickname=cfg.get("nickname") or target,
                       avatar=avatar_url(target) if target else "",
                       photos_dir=_full_rel(context["root"], cfg.get("photos_dir") or "imgs"),
                       videos_dir=_full_rel(context["root"], cfg.get("videos_dir") or "videos"),
                       data_dir=_full_rel(context["root"], cfg.get("data_dir") or "datas"),
                       download_photos=bool(cfg.get("download_photos", True)),
                       download_videos=bool(cfg.get("download_videos", True)), source=cfg.get("source", ""),
                       profile_id=context["id"])

    @app.post("/api/config/settings")
    def settings():
        context, error = active_ctx()
        if error:
            return error
        if context["engine"].is_alive():
            return jsonify(error="采集进行中，停止后再修改设置"), 409
        payload = request.get_json(silent=True) or {}
        cfg = cookiemgr.load(context["root"])
        for key in ("photos_dir", "videos_dir", "data_dir"):
            if key in payload and str(payload[key]).strip():
                value = str(payload[key]).strip()
                if Path(value).is_absolute() or ".." in Path(value).parts:
                    return jsonify(error="保存路径必须在当前账号目录内"), 400
                cfg[key] = _strip_root(context["root"], value)
        for key in ("download_photos", "download_videos"):
            if key in payload:
                cfg[key] = bool(payload[key])
        cookiemgr.save(context["root"], cfg)
        return jsonify(ok=True)

    @app.get("/media/<kind>/<path:name>")
    def media(kind, name):
        context, error = active_ctx()
        if error:
            return error
        if kind not in {"photos", "videos", "avatars"}:
            return jsonify(error="类型无效"), 400
        cfg = cookiemgr.load(context["root"])
        base = context["root"] / (cfg.get("videos_dir") if kind == "videos" else cfg.get("photos_dir"))
        if kind == "avatars":
            base /= "avatars"
        path = (base / name).resolve()
        if not _inside(base, path) or not path.is_file():
            return jsonify(error="文件不存在"), 404
        return send_file(path, conditional=True)

    return app
