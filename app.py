"""
Web cào bình luận Facebook: người dùng chỉ dán link bài viết -> bấm chạy -> tải CSV.
Không cần file HAR: fb_browser.py tự mở bài viết bằng Chrome ẩn để lấy session.

  python app.py                       # http://127.0.0.1:8765  (trang quản trị: /admin)
  python app.py --host 0.0.0.0        # cho máy khác truy cập
  ADMIN_TOKEN=... python app.py       # đặt mật khẩu admin (mặc định tự sinh, lưu ở data/admin_token.txt)
"""

import argparse
import asyncio
import csv
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, Response as RawResponse

import fb_browser
import ig
import scraper as core
import threads

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FB_SCRAPER_DATA", os.path.join(BASE_DIR, "data"))
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
JOBS_PATH = os.path.join(DATA_DIR, "jobs.json")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")  # one Facebook session per end user (chứa cookie): KHÔNG chia sẻ
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")  # có thể chứa cookie Facebook: không chia sẻ
TOKEN_PATH = os.path.join(DATA_DIR, "admin_token.txt")

OWNER_COOKIE = "fbcs_uid"
ACTIVE = ("resolving", "running")
PERSIST_KEYS = ("id", "owner", "platform", "url", "status", "message", "created_at", "started_at", "finished_at",
                "start_count", "feedback_id", "post_id", "title", "total_hint", "count", "mode", "streams", "stats", "meta")

# Each social platform has its own internal API/auth; only enabled ones can be scraped. Add one here + a
# scraper backend to turn it on. The UI builds its tabs from this list.
PLATFORMS = [
    {"id": "facebook", "name": "Facebook", "hosts": ["facebook.com", "fb.com", "fb.watch", "fb.me"], "enabled": True},
    {"id": "threads", "name": "Threads", "hosts": ["threads.net", "threads.com"], "enabled": True},
    {"id": "instagram", "name": "Instagram", "hosts": ["instagram.com", "instagr.am"], "enabled": True},
    {"id": "x", "name": "X (Twitter)", "hosts": ["x.com", "twitter.com"], "enabled": False},
]
PLATFORM_BY_ID = {p["id"]: p for p in PLATFORMS}


def platform_of(target):
    """Which platform a pasted link / id belongs to, or None if unrecognized."""
    t = (target or "").strip()
    if t.isdigit() or t.startswith(core.FEEDBACK_PREFIX):
        return "facebook"
    host = (urlparse(t if "://" in t else "https://" + t).hostname or "").lower()
    for p in PLATFORMS:
        if any(host == h or host.endswith("." + h) for h in p["hosts"]):
            return p["id"]
    return None
DEFAULT_SETTINGS = {"parallel": 2, "concurrency": 20, "delay": 0.0, "proxies": [], "fb_cookie": "", "max_links": 20}

state = {"settings": dict(DEFAULT_SETTINGS), "admin_token": None, "closing": False, "login": None}
jobs = {}     # id -> job dict, in submission order
runtime = {}  # id -> {"task": asyncio.Task, "scraper": core.Scraper | None}
pool = fb_browser.BrowserPool(state_path=fb_browser.STATE_PATH)
threads_pool = threads.ThreadsPool()
ig_pool = ig.IgPool()


# ----------------------------------------------------------------- persistence

def job_dir(jid):
    return os.path.join(JOBS_DIR, jid)


def write_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def save_jobs():
    write_json(JOBS_PATH, [{k: j.get(k) for k in PERSIST_KEYS} for j in jobs.values()])


def admin_token():
    """ADMIN_TOKEN env var, else a random token generated once and kept in data/admin_token.txt."""
    token = os.environ.get("ADMIN_TOKEN")
    if token:
        return token
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        token = secrets.token_urlsafe(12)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(token)
    return token


def load_all():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            state["settings"] = {**DEFAULT_SETTINGS, **json.load(f)}
    state["admin_token"] = admin_token()
    if os.path.exists(JOBS_PATH):
        with open(JOBS_PATH, encoding="utf-8") as f:
            for j in json.load(f):
                j = {k: j.get(k) for k in PERSIST_KEYS}
                j["log"] = deque(maxlen=30)
                if j["status"] in ACTIVE or j["status"] == "queued":
                    j.update(status="stopped", message="Server đã tắt giữa chừng. Bấm Tiếp tục để chạy tiếp.")
                jobs[j["id"]] = j


# ------------------------------------------------ per-user Facebook sessions

SESSION_TTL_DAYS = 30  # stored cookies auto-expire after this many days


def _cipher():
    """Fernet keyed by data/.session_key (created once). Encrypts stored cookies at rest."""
    from cryptography.fernet import Fernet  # lazy: only needed when a user saves a session
    keyfile = os.path.join(DATA_DIR, ".session_key")
    try:
        key = open(keyfile, "rb").read()
    except FileNotFoundError:
        os.makedirs(DATA_DIR, exist_ok=True)
        key = Fernet.generate_key()
        with open(keyfile, "wb") as f:
            f.write(key)
    return Fernet(key)


def user_session_path(uid, platform):
    return os.path.join(SESSIONS_DIR, f"{uid}.{platform}.json")


def _legacy_fb_path(uid):  # Facebook sessions saved before per-platform storage
    return os.path.join(SESSIONS_DIR, f"{uid}.json")


def save_user_session(uid, platform, account_id, cookie):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    write_json(user_session_path(uid, platform), {"account_id": account_id, "added_at": time.time(),
                                                  "cookie_enc": _cipher().encrypt(cookie.encode()).decode()})


def clear_user_session(uid, platform):
    for path in (user_session_path(uid, platform),
                 _legacy_fb_path(uid) if platform == "facebook" else None):
        try:
            os.remove(path) if path else None
        except FileNotFoundError:
            pass


def load_user_session(uid, platform):
    """Decrypt a user's stored session for one platform, or None; drops it once past the TTL."""
    if not uid:
        return None
    path = user_session_path(uid, platform)
    if not os.path.exists(path) and platform == "facebook" and os.path.exists(_legacy_fb_path(uid)):
        path = _legacy_fb_path(uid)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if time.time() - data.get("added_at", 0) > SESSION_TTL_DAYS * 86400:
        clear_user_session(uid, platform)
        return None
    try:
        data["cookie"] = _cipher().decrypt(data["cookie_enc"].encode()).decode()
    except Exception:  # key rotated / corrupt / tampered -> treat as no session
        return None
    return data


@asynccontextmanager
async def lifespan(_app):
    load_all()
    yield
    # Ctrl+C: stop gracefully so every scraper saves its resume state
    state["closing"] = True
    tasks = [rt["task"] for rt in runtime.values()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    save_jobs()
    await pool.close()
    await threads_pool.close()
    await ig_pool.close()


app = FastAPI(title="FB Comment Scraper", lifespan=lifespan)


# --------------------------------------------------------------------- runner

def pump():
    """Start queued jobs while fewer than `parallel` are active."""
    if state["closing"]:
        return
    active = sum(j["status"] in ACTIVE for j in jobs.values())
    for j in jobs.values():
        if active >= state["settings"]["parallel"]:
            break
        if j["status"] == "queued":
            j.update(status="resolving", message=None)
            runtime[j["id"]] = {"task": asyncio.create_task(run_job(j)), "scraper": None}
            active += 1


async def _prepare(j, plat, st, proxies):
    """Open the post and build its scraper, per platform. Returns (info, scraper)."""
    jid = j["id"]
    p = fb_browser.pick_proxy(proxies)
    cookie = (load_user_session(j.get("owner"), plat) or {}).get("cookie")  # the job owner's session for this platform
    if plat == "facebook":
        cookie = cookie or st["fb_cookie"] or None
        info = await pool.open_post(j["url"], feedback_id=j.get("feedback_id"), cookie=cookie, proxy=p)
        s = core.Scraper(info["templates"], job_dir(jid), feedback_id=info["feedback_id"],
                         concurrency=st["concurrency"], delay=st["delay"], proxies=proxies,
                         log=j["log"].append, log_progress=False, debug_dir=os.path.join(job_dir(jid), "debug"))
    elif plat == "threads":  # logged in via an Instagram/Threads cookie if the user added one, else guest
        session = await threads_pool.new_session(j["url"], cookie=cookie, proxy=p)
        info = session.info
        s = threads.ThreadsScraper(session, job_dir(jid), delay=max(st["delay"], 0.6),
                                   log=j["log"].append, log_progress=False)
    else:  # instagram — requires an Instagram login cookie
        session = await ig_pool.new_session(j["url"], cookie=cookie, proxy=p)
        info = session.info
        # IG: ~0.35s/request by default (faster); admin can set a bigger "delay" if rate-limited, or smaller to risk it
        s = ig.IgScraper(session, job_dir(jid), delay=(st["delay"] or 0.35),
                         log=j["log"].append, log_progress=False, debug_dir=os.path.join(job_dir(jid), "debug"))
    return info, s


async def run_job(j):
    jid, s = j["id"], None
    plat = j.get("platform", "facebook")
    if plat not in ("facebook", "threads", "instagram"):
        name = PLATFORM_BY_ID.get(plat, {}).get("name", "Nền tảng này")
        j.update(status="error", message=f"{name} đang phát triển, chưa cào được.", finished_at=time.time())
        runtime.pop(jid, None)
        save_jobs()
        pump()
        return
    st = dict(state["settings"])
    proxies = st["proxies"] or None
    try:
        info, s = await _prepare(j, plat, st, proxies)
        for k in ("feedback_id", "post_id", "title", "total_hint", "mode"):
            if info.get(k) is not None:
                j[k] = info[k]
        runtime[jid]["scraper"] = s
        j.update(status="running", started_at=time.time(), start_count=len(s.seen), message=None)
        save_jobs()
        await s.run()

        j["status"] = "done"
        if not s.seen:
            if j.get("total_hint") == 0:
                j["message"] = "Bài viết không có bình luận/trả lời."
            else:
                j.update(status="error", message="Không lấy được gì. Bài có thể bị giới hạn người xem hoặc "
                                                 "nền tảng đang chặn; thử lại sau, thêm proxy hoặc đăng nhập.")
        elif getattr(s, "reply_failed", 0):
            j["message"] = f"{s.reply_failed} chuỗi reply bị lỗi. Bấm Tiếp tục để thử lại phần còn thiếu."
    except asyncio.CancelledError:
        j.update(status="stopped", message="Đã dừng. Bấm Tiếp tục để chạy tiếp từ chỗ cũ.")
    except Exception as e:
        j.update(status="error", message=cookie_error_message(plat, str(e)))
    finally:
        if s is not None:
            j.update(count=len(s.seen), streams=streams_view(s), meta=getattr(s, "meta", {}) or {})
            try:
                j["stats"] = core.completeness(job_dir(jid))
            except OSError:
                pass
        j["finished_at"] = time.time()
        runtime.pop(jid, None)
        save_jobs()
        pump()


COOKIE_ERR_HINTS = ("đăng nhập", "login", "checkpoint", "cookie", "session hết hạn", "sessionid",
                    "trang đăng nhập", "cookie chưa nhận")


def cookie_error_message(platform, err):
    """If a job failed because the login cookie is bad, tell the user clearly to update it."""
    err = err[:400]
    if any(h in err.lower() for h in COOKIE_ERR_HINTS) and platform in fb_browser.PLATFORM_AUTH:
        name = PLATFORM_BY_ID.get(platform, {}).get("name", platform)
        return (f"⚠️ Cookie {name} có vấn đề (hết hạn hoặc sai). Vào tab {name} → cập nhật cookie mới "
                f"(đăng nhập lại rồi export), sau đó chạy lại. Chi tiết: {err}")
    return err


def streams_view(s):
    streams = getattr(s, "streams", None)
    if isinstance(streams, dict) and streams:  # Facebook: one stream per sort order
        return [{"order": core.INTENT_NAMES.get(x["intent"], x["intent"]), "pages": x["pages"], "new": x["new"],
                 "stop": x["stop"], "last_size": x["last_size"]} for x in streams.values()]
    # Threads / simple scrapers: a single reply stream
    return [{"order": "replies", "pages": getattr(s, "pages", 0), "new": len(getattr(s, "seen", ())),
             "stop": "end" if getattr(s, "done", False) else None, "last_size": None}]


# ----------------------------------------------------------- users and admin

def owner_id(request: Request, response: Response):
    """Each browser gets an anonymous id (cookie) and only sees its own jobs."""
    uid = request.cookies.get(OWNER_COOKIE)
    if not uid or not re.fullmatch(r"[0-9a-f]{32}", uid):
        uid = uuid.uuid4().hex
        response.set_cookie(OWNER_COOKIE, uid, max_age=365 * 86400, httponly=True, samesite="lax")
    return uid


def is_admin(request: Request):
    token = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    return bool(token) and secrets.compare_digest(token, state["admin_token"])


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(401, "Sai mật khẩu admin.")


def get_job(jid, uid, request):
    j = jobs.get(jid)
    if not j or (j.get("owner") != uid and not is_admin(request)):
        raise HTTPException(404, "Không tìm thấy job.")
    return j


def job_view(j):
    v = {k: j.get(k) for k in PERSIST_KEYS if k != "owner"}
    v["platform"] = v.get("platform") or "facebook"
    v["log"] = list(j["log"])[-3:]
    v["count"] = v["count"] or 0
    end = time.time() if j["status"] in ACTIVE else j.get("finished_at")
    v["elapsed"] = end - j["started_at"] if j.get("started_at") and end and end >= j["started_at"] else None
    s = runtime.get(j["id"], {}).get("scraper")
    if s:
        v.update(count=len(s.seen), requests=s.requests, top_pages=s.top_pages, top_done=s.top_done,
                 reply_pending=s.reply_pending, reply_failed=s.reply_failed, streams=streams_view(s))
    return v


def num(value, default, lo, hi, cast=int):
    try:
        return min(max(cast(value), lo), hi)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------- pages

def public_base(request: Request) -> str:
    """URL công khai của trang (để chèn vào thẻ Open Graph khi gửi link).

    Ưu tiên biến môi trường PUBLIC_URL (đặt thủ công cho chắc, vd link Tailscale
    Funnel/Cloudflare). Nếu không có, suy ra từ header do tunnel chuyển tới."""
    env = (os.environ.get("PUBLIC_URL") or "").strip()
    if env:
        return env.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host")
            or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


@app.get("/")
async def index(request: Request):
    # đọc file mỗi lần để sửa giao diện là F5 thấy ngay (không cần restart app)
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__PUBLIC_URL__", public_base(request))
    resp = HTMLResponse(html)
    owner_id(request, resp)
    return resp


@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "admin.html"))


# ảnh thẻ xem trước (Open Graph). Tạo 1 lần bằng Pillow nếu có; thiếu Pillow thì
# bỏ qua (link vẫn hiện thẻ chữ, chỉ không có ảnh).
_OG_PATH = os.path.join(BASE_DIR, "static", "og.png")

def _ensure_og_image() -> bool:
    if os.path.exists(_OG_PATH):
        return True
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    try:
        W, H = 1200, 630
        img = Image.new("RGB", (W, H), "#1b74e4")
        d = ImageDraw.Draw(img)
        d.rectangle([0, H - 150, W, H], fill="#1559b0")

        def font(sz):
            for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
                try:
                    return ImageFont.truetype(name, sz)
                except Exception:
                    continue
            return ImageFont.load_default()

        d.text((80, 150), "Cào bình luận", font=font(96), fill="#ffffff")
        d.text((80, 270), "mạng xã hội", font=font(96), fill="#ffffff")
        d.text((80, 470), "Facebook · Threads · Instagram  →  Excel / CSV",
               font=font(40), fill="#dbe7ff")
        img.save(_OG_PATH, "PNG")
        return True
    except Exception:
        return False


@app.get("/og.png")
async def og_image():
    if _ensure_og_image():
        return FileResponse(_OG_PATH, media_type="image/png")
    return RawResponse(status_code=404)


# ------------------------------------------------------------------ user API

@app.get("/api/status")
async def status():
    st = state["settings"]
    return {"max_links": st["max_links"], "account_mode": bool(st["fb_cookie"]) or pool.has_session,
            "platforms": [{"id": p["id"], "name": p["name"], "enabled": p["enabled"]} for p in PLATFORMS]}


def _login_platform(platform):
    if platform not in fb_browser.PLATFORM_AUTH or not PLATFORM_BY_ID.get(platform, {}).get("enabled"):
        raise HTTPException(400, "Nền tảng không hỗ trợ đăng nhập.")
    return platform


@app.get("/api/session")
async def my_session(platform: str = "facebook", uid: str = Depends(owner_id)):
    _login_platform(platform)
    s = load_user_session(uid, platform)
    shared = (pool.has_session or bool(state["settings"]["fb_cookie"])) if platform == "facebook" else False
    return {"account_id": (s or {}).get("account_id"), "has_session": bool(s), "ttl_days": SESSION_TTL_DAYS,
            "platform": platform, "shared": shared}


@app.post("/api/session")
async def set_my_session(request: Request, platform: str = "facebook", uid: str = Depends(owner_id)):
    _login_platform(platform)
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        account, cookie = fb_browser.normalize_user_session(raw, platform)
    except ValueError as e:
        raise HTTPException(400, str(e)[:300])
    except (json.JSONDecodeError, TypeError, KeyError):
        raise HTTPException(400, "Nội dung không hợp lệ: cần chuỗi cookie hoặc JSON export.")
    try:
        save_user_session(uid, platform, account, cookie)
    except ImportError:
        raise HTTPException(500, "Thiếu thư viện mã hoá. Chạy: python -m pip install cryptography")
    return {"account_id": account}


@app.delete("/api/session")
async def delete_my_session(platform: str = "facebook", uid: str = Depends(owner_id)):
    _login_platform(platform)
    clear_user_session(uid, platform)
    return {"ok": True}


@app.get("/api/jobs")
async def list_jobs(request: Request, all: int = 0, uid: str = Depends(owner_id)):
    admin = bool(all) and is_admin(request)
    return {"jobs": [job_view(j) for j in jobs.values() if admin or j.get("owner") == uid], "admin": admin}


@app.post("/api/jobs")
async def create_jobs(request: Request, uid: str = Depends(owner_id)):
    body = await request.json()
    urls = []
    for line in body.get("urls") or []:
        line = str(line).strip()
        if line and not line.startswith("#") and line not in urls:
            urls.append(line)
    if not urls:
        raise HTTPException(400, "Danh sách link trống.")
    detected = {u: platform_of(u) for u in urls}
    unknown = [u for u, p in detected.items() if p is None]
    if unknown:
        raise HTTPException(400, "Không nhận ra nền tảng của link: " + ", ".join(unknown[:3]))
    disabled = sorted({PLATFORM_BY_ID[p]["name"] for p in detected.values() if not PLATFORM_BY_ID[p]["enabled"]})
    if disabled:
        raise HTTPException(400, f"{', '.join(disabled)} đang phát triển, chưa cào được. Hiện chỉ hỗ trợ Facebook.")
    limit = state["settings"]["max_links"]
    pending = sum(1 for j in jobs.values() if j.get("owner") == uid and (j["status"] in ACTIVE or j["status"] == "queued"))
    if len(urls) + pending > limit:
        raise HTTPException(400, f"Tối đa {limit} bài đang chạy/chờ cùng lúc (bạn đang có {pending}).")
    for url in urls:
        jid = uuid.uuid4().hex[:10]
        jobs[jid] = {"id": jid, "owner": uid, "platform": detected[url], "url": url, "status": "queued",
                     "created_at": time.time(), "log": deque(maxlen=30)}
    save_jobs()
    pump()
    return {"created": len(urls)}


@app.post("/api/jobs/{jid}/stop")
async def stop_job(jid: str, request: Request, uid: str = Depends(owner_id)):
    j = get_job(jid, uid, request)
    if j["status"] == "queued":
        j.update(status="stopped", message="Đã huỷ trước khi chạy.")
        save_jobs()
    elif jid in runtime:
        runtime[jid]["task"].cancel()
    return {"ok": True}


@app.post("/api/jobs/{jid}/resume")
async def resume_job(jid: str, request: Request, uid: str = Depends(owner_id)):
    j = get_job(jid, uid, request)
    if j["status"] not in ACTIVE and j["status"] != "queued":
        j.update(status="queued", message=None)
        save_jobs()
        pump()
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
async def delete_job(jid: str, request: Request, uid: str = Depends(owner_id)):
    j = get_job(jid, uid, request)
    if j["status"] in ACTIVE:
        raise HTTPException(409, "Dừng job trước khi xoá.")
    jobs.pop(jid)
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    save_jobs()
    return {"ok": True}


@app.post("/api/stop-all")
async def stop_all(request: Request, all: int = 0, uid: str = Depends(owner_id)):
    mine = [j for j in jobs.values() if (all and is_admin(request)) or j.get("owner") == uid]
    for j in mine:  # cancel the queue first so pump() doesn't start them
        if j["status"] == "queued":
            j.update(status="stopped", message="Đã huỷ trước khi chạy.")
    for j in mine:
        if j["id"] in runtime:
            runtime[j["id"]]["task"].cancel()
    save_jobs()
    return {"ok": True}


@app.get("/api/jobs/{jid}/download")
async def download(jid: str, request: Request, fmt: str = "csv", uid: str = Depends(owner_id)):
    j = get_job(jid, uid, request)
    d = job_dir(jid)
    src = os.path.join(d, "comments.jsonl")
    if not os.path.exists(src):
        raise HTTPException(404, "Chưa có dữ liệu.")
    name = f"comments_{j.get('post_id') or jid}"
    if fmt == "jsonl":
        return FileResponse(src, filename=name + ".jsonl", media_type="application/x-ndjson")
    if fmt == "xlsx":
        dst = os.path.join(d, "comments.xlsx")
        if not await asyncio.to_thread(build_xlsx, [(jid, j.get("title"))], dst):
            raise HTTPException(404, "Chưa có dữ liệu.")
        return FileResponse(dst, filename=_safe_filename(j.get("title") or name) + ".xlsx", media_type=XLSX_MEDIA)
    if not await asyncio.to_thread(core.export_csv, d):
        raise HTTPException(404, "Chưa có dữ liệu.")
    return FileResponse(os.path.join(d, "comments.csv"), filename=name + ".csv", media_type="text/csv")


# ----------------------------------------------------------------- Excel export

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _fmt_time(iso):
    """ISO (UTC) -> 'dd/mm/yyyy HH:MM' in Vietnam time (UTC+7)."""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        return datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return str(iso)


def _safe_filename(name, fallback="comments"):
    return re.sub(r'[\\/:*?"<>|\r\n]+', " ", str(name or "")).strip()[:100] or fallback


def _sheet_name(title, used, fallback):
    name = re.sub(r'[\\/?*\[\]:]+', " ", str(title or "")).strip()[:31] or fallback[:31]
    base, i = name, 2
    while name.lower() in used:
        suffix = f" ({i})"
        name = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(name.lower())
    return name


def _threaded(rows):
    """(depth, reply_label, author, text, time) in threaded order: each comment then its numbered replies."""
    from collections import defaultdict
    children = defaultdict(list)
    for r in rows:
        if r.get("parent_id"):
            children[r["parent_id"]].append(r)
    out, seen = [], set()

    def walk(r, depth, label):
        rid = r.get("id")
        if rid in seen:
            return
        seen.add(rid)
        out.append((depth, label, r.get("author_name") or "", r.get("text") or "", r.get("created_time")))
        for i, child in enumerate(children.get(rid, []), 1):
            walk(child, depth + 1, f"Reply {i}")

    for r in rows:
        if not r.get("parent_id"):
            walk(r, 0, None)
    for r in rows:                       # replies whose parent is missing -> show on their own line
        if r.get("id") not in seen:
            walk(r, 0, None)
    return out


def _write_sheet(ws, rows):
    from openpyxl.styles import Alignment, Font
    ws.append(["Bình luận", "Thời gian"])
    ws["A1"].font = ws["B1"].font = Font(bold=True)
    for depth, label, author, text, t in _threaded(rows):
        head = f"{label} — " if label else ""
        body = f"{author}: {text}" if author else text
        ws.append([head + body, _fmt_time(t)])
        cell = ws.cell(ws.max_row, 1)
        cell.alignment = Alignment(indent=min(depth, 8), wrap_text=True, vertical="top")
        if depth == 0:
            cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 95
    ws.column_dimensions["B"].width = 18
    ws.freeze_panes = "A2"


def build_xlsx(entries, dst):
    """entries: [(job_id, title)] -> an .xlsx with one sheet per post, each sheet named after its title."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    used, n = set(), 0
    for jid, title in entries:
        rows = list(core.iter_jsonl(os.path.join(job_dir(jid), "comments.jsonl")))
        if not rows:
            continue
        _write_sheet(wb.create_sheet(_sheet_name(title, used, "post " + jid)), rows)
        n += 1
    if not n:
        return 0
    wb.save(dst)
    return n


def build_export(entries, dst):
    n, writer = 0, None
    with open(dst, "w", newline="", encoding="utf-8-sig") as f:
        for jid, url, post_id in entries:
            src = os.path.join(job_dir(jid), "comments.jsonl")
            if not os.path.exists(src):
                continue
            for rec in core.iter_jsonl(src):
                row = {"post_url": url, "post_id": post_id, **rec}
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=list(row), extrasaction="ignore")
                    writer.writeheader()
                writer.writerow(row)
                n += 1
    return n


@app.get("/api/export-all")
async def export_all(request: Request, all: int = 0, platform: str = "", uid: str = Depends(owner_id)):
    admin = bool(all) and is_admin(request)
    entries = [(j["id"], j["url"], j.get("post_id")) for j in jobs.values()
               if (admin or j.get("owner") == uid) and (not platform or j.get("platform", "facebook") == platform)]
    os.makedirs(os.path.join(DATA_DIR, "exports"), exist_ok=True)
    dst = os.path.join(DATA_DIR, "exports", f"{'all' if admin else uid}.csv")
    if not await asyncio.to_thread(build_export, entries, dst):
        raise HTTPException(404, "Chưa có dữ liệu.")
    return FileResponse(dst, filename=f"comments_{time.strftime('%Y%m%d_%H%M')}.csv", media_type="text/csv")


@app.get("/api/export-xlsx")
async def export_xlsx(request: Request, all: int = 0, platform: str = "", uid: str = Depends(owner_id)):
    admin = bool(all) and is_admin(request)
    entries = [(j["id"], j.get("title") or j.get("url")) for j in jobs.values()
               if (admin or j.get("owner") == uid) and (not platform or j.get("platform", "facebook") == platform)]
    os.makedirs(os.path.join(DATA_DIR, "exports"), exist_ok=True)
    dst = os.path.join(DATA_DIR, "exports", f"{'all' if admin else uid}.xlsx")
    if not await asyncio.to_thread(build_xlsx, entries, dst):
        raise HTTPException(404, "Chưa có dữ liệu.")
    return FileResponse(dst, filename=f"comments_{time.strftime('%Y%m%d_%H%M')}.xlsx", media_type=XLSX_MEDIA)


# ----------------------------------------------------------------- admin API

def settings_view():
    st = state["settings"]
    return {**{k: v for k, v in st.items() if k != "fb_cookie"}, "fb_cookie_set": bool(st["fb_cookie"])}


@app.get("/api/admin/settings")
async def get_settings(request: Request):
    require_admin(request)
    return settings_view()


@app.post("/api/admin/settings")
async def update_settings(request: Request):
    require_admin(request)
    body = await request.json()
    st = state["settings"]
    st.update(
        parallel=num(body.get("parallel"), st["parallel"], 1, 20),
        concurrency=num(body.get("concurrency"), st["concurrency"], 1, 100),
        delay=num(body.get("delay"), st["delay"], 0.0, 30.0, float),
        max_links=num(body.get("max_links"), st["max_links"], 1, 500),
    )
    if "proxies" in body:
        st["proxies"] = [str(p).strip() for p in body["proxies"] or [] if str(p).strip()]
    if body.get("clear_cookie"):
        st["fb_cookie"] = ""
    elif str(body.get("fb_cookie") or "").strip():
        st["fb_cookie"] = str(body["fb_cookie"]).strip()
    write_json(SETTINGS_PATH, st)
    pump()  # a higher `parallel` can start queued jobs right away
    return settings_view()


@app.post("/api/admin/check")
async def check_post(request: Request):
    """Open one post exactly like a job would and report what was found (no scraping)."""
    require_admin(request)
    url = str((await request.json()).get("url") or "").strip()
    if platform_of(url) != "facebook":
        raise HTTPException(400, "Cần một link bài viết Facebook.")
    st = state["settings"]
    t = time.time()
    try:
        info = await pool.open_post(url, cookie=st["fb_cookie"] or None, proxy=fb_browser.pick_proxy(st["proxies"]))
    except Exception as e:
        raise HTTPException(400, f"{e!s:.400}")
    return {"elapsed": round(time.time() - t, 1), "browser": pool.channel, "mode": info["mode"],
            "post_id": info["post_id"], "title": info["title"], "total_hint": info["total_hint"],
            "queries": {name: tpl["form"]["doc_id"] for name, tpl in info["templates"].items()}}


@app.get("/api/admin/session")
async def admin_session(request: Request):
    require_admin(request)
    return {"account_id": fb_browser.account_id(fb_browser.STATE_PATH), "has_session": pool.has_session,
            "login": state["login"]}


async def _run_login(proxy):
    try:
        uid = await fb_browser.login(fb_browser.STATE_PATH, proxy=proxy)
        state["login"] = {"status": "done", "account_id": uid}
    except Exception as e:
        state["login"] = {"status": "error", "error": f"{e!s:.300}"}


@app.post("/api/admin/login")
async def admin_login(request: Request):
    """Open a login window ON THE HOST machine; the person signs in there, the session is saved.

    Only useful when the admin is at the computer running the app (e.g. localhost): the browser
    window appears there, not in the admin's own browser.
    """
    require_admin(request)
    if isinstance(state["login"], dict) and state["login"].get("status") == "running":
        raise HTTPException(409, "Đang chờ đăng nhập ở cửa sổ trình duyệt trên máy chủ.")
    state["login"] = {"status": "running"}
    asyncio.create_task(_run_login(fb_browser.pick_proxy(state["settings"]["proxies"])))
    return {"ok": True, "message": "Đã mở cửa sổ đăng nhập trên máy đang chạy app. Đăng nhập xong sẽ tự lưu phiên."}


@app.post("/api/admin/session/upload")
async def admin_session_upload(request: Request):
    """Save a login session produced elsewhere (fb_state.json), for servers with no screen."""
    require_admin(request)
    body = (await request.body()).decode("utf-8", "replace")
    try:
        uid = fb_browser.save_session(body, fb_browser.STATE_PATH)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"{e!s:.300}")
    state["login"] = {"status": "done", "account_id": uid}
    return {"ok": True, "account_id": uid}


@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    require_admin(request)
    fb_browser.clear_session(fb_browser.STATE_PATH)
    state["login"] = None
    return {"ok": True}


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="FB Comment Scraper - web")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="không tự mở trình duyệt khi khởi động")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    print(f"FB Comment Scraper: {url}   |   admin: {url}/admin   |   Ctrl+C để tắt")
    print(f"Mật khẩu admin: {admin_token()}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
