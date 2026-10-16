"""
Lấy session Facebook tự động bằng Chrome chạy ẩn, thay cho file HAR.

Mỗi bài viết được mở như một người xem chưa đăng nhập (hoặc bằng cookie tài khoản nếu admin cấu hình).
Ngay trong trang, Facebook có sẵn module nội bộ (`require`) chứa doc_id và tên biến của từng query,
token lsd/fb_dtsg và tham số request. Ta đọc chúng ra, dựng template rồi giao cho scraper.py gọi bằng httpx.
"""

import asyncio
import json
import os
import random
import re
import time
from urllib.parse import unquote, urlparse

import httpx
from playwright.async_api import async_playwright

import scraper as core

ROOT_QUERY = "CommentListComponentsRootQuery"
QUERIES = (core.TOP_QUERY, *core.REPLY_QUERIES, ROOT_QUERY)
ASBD_ID = "359341"
BLOCKED_RESOURCES = {"image", "media", "font"}
FB_HOSTS = ("facebook.com", "fb.com", "fb.watch", "fb.me")
NAV_HOSTS = FB_HOSTS + ("fbcdn.net", "fbsbx.com")  # the browser never navigates anywhere else (no SSRF via links)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FB_SCRAPER_DATA", os.path.join(BASE_DIR, "data"))
STATE_PATH = os.path.join(DATA_DIR, "fb_state.json")  # phiên đăng nhập đã lưu (chứa cookie): KHÔNG chia sẻ file này

# Variables the page itself would fill in; the variable *names* always come from the query module.
VAR_DEFAULTS = {
    "feedLocation": "POST_PERMALINK_DIALOG", "scale": 1, "useDefaultActor": False, "feedbackSource": 2,
    "commentsAfterCount": -1, "commentsIntentToken": core.ALL_COMMENTS_INTENT,
}

PAGE_JS = r"""
(names) => {
  if (typeof require !== 'function') return null;
  const req = n => { try { return require(n); } catch (e) { return undefined; } };
  const mods = {};
  for (const n of names) {
    const docId = req(n + '_facebookRelayOperation');
    const g = req(n + '.graphql');
    if (!docId || !g) continue;
    const pv = (g.params && g.params.providedVariables) || {};
    mods[n] = {
      doc_id: String(docId),
      args: ((g.fragment || {}).argumentDefinitions || []).map(a => a.name),
      provided: Object.fromEntries(Object.entries(pv).map(([k, m]) => {
        try { return [k, m.get()]; } catch (e) { return [k, null]; }
      })),
    };
  }
  let params = null;
  try { const gap = req('getAsyncParams'); params = gap ? gap('POST') : null; } catch (e) {}
  return {
    mods, params,
    lsd: (req('LSD') || {}).token || null,
    dtsg: (req('DTSGInitialData') || {}).token || null,
    user: String((req('CurrentUserInitialData') || {}).USER_ID || '0'),
  };
}
"""

_mods_cache = {}  # doc_ids are global per Facebook release: reuse them for pages that don't load every module


class LoginRequired(RuntimeError):
    pass


def host_in(url, hosts):
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def normalize_target(target):
    t = target.strip()
    if t.startswith(core.FEEDBACK_PREFIX):
        t = core.decode_feedback(t) or t
    if t.isdigit():
        return f"https://www.facebook.com/{t}"
    if "://" not in t:
        t = "https://" + t
    return re.sub(r"^https?://(?:m|mbasic|touch|web)\.facebook\.com", "https://www.facebook.com", t)


def chrome_ua(version):
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version.split('.')[0]}.0.0.0 Safari/537.36")


def parse_cookie_string(raw, default_domain=".facebook.com"):
    """'c_user=1; xs=abc', a Cookie-Editor JSON export, or a storage_state JSON -> Playwright cookies.

    A JSON export keeps each cookie's own domain; a bare string gets `default_domain` (per platform).
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    out = []
    if raw[0] in "[{":
        data = json.loads(raw)
        items = data.get("cookies", []) if isinstance(data, dict) else data
        for c in items:
            if isinstance(c, dict) and c.get("name"):
                out.append({"name": c["name"], "value": c.get("value", ""),
                            "domain": c.get("domain") or default_domain, "path": c.get("path") or "/",
                            "secure": True})
    else:
        for p in raw.split(";"):
            if "=" in p:
                k, v = p.strip().split("=", 1)
                out.append({"name": k, "value": v, "domain": default_domain, "path": "/", "secure": True})
    return out


def playwright_proxy(url):
    if not url:
        return None
    u = urlparse(url)
    proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        proxy.update(username=unquote(u.username), password=unquote(u.password or ""))
    return proxy


def build_templates(info, cookie_header, user_agent, referer):
    """Turn what the page exposed into the same template shape a HAR produces."""
    user = info.get("user") or "0"
    base = {k: str(v) for k, v in (info.get("params") or {}).items()}
    base.update(av=user, __user=user, __a="1", fb_api_caller_class="RelayModern", server_timestamps="true")
    base.setdefault("__comet_req", "15")
    if info.get("lsd"):
        base["lsd"] = info["lsd"]
    if info.get("dtsg"):
        base["fb_dtsg"] = info["dtsg"]
    base.setdefault("jazoest", "2" + str(sum(map(ord, base.get("fb_dtsg") or base.get("lsd") or ""))))

    headers = {
        "user-agent": user_agent, "accept": "*/*", "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded", "origin": "https://www.facebook.com",
        "referer": referer, "x-asbd-id": ASBD_ID, "cookie": cookie_header,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
    }
    if info.get("lsd"):
        headers["x-fb-lsd"] = info["lsd"]

    templates = {}
    for name, m in info["mods"].items():
        variables = {a: None for a in m["args"]}
        variables.update({k: v for k, v in VAR_DEFAULTS.items() if k in variables})
        variables.update(m["provided"])
        templates[name] = {
            "headers": dict(headers, **{"x-fb-friendly-name": name}),
            "form": dict(base, fb_api_req_friendly_name=name, doc_id=m["doc_id"], variables=json.dumps(variables)),
            "variables": variables,
        }
    return templates


async def root_total(templates, feedback_id, proxy=None):
    """One RootQuery call: confirms the feedback id and returns the total comment count (incl. replies).

    Returns None when the check could not run, False when Facebook returned no such post.
    """
    tpl = templates.get(ROOT_QUERY)
    if not tpl:
        return None
    variables = dict(tpl["variables"], id=feedback_id)
    try:
        async with httpx.AsyncClient(http2=True, proxy=proxy, timeout=30) as client:
            r = await client.post(core.GQL_URL, headers=tpl["headers"],
                                  data=dict(tpl["form"], variables=json.dumps(variables)))
    except httpx.HTTPError:
        return None
    for chunk in core.parse_chunks(r.text):
        if "data" in chunk:
            node = core.dig(chunk, "data", "node")
            if not node:
                return False
            return core.dig(node, "comment_rendering_instance_for_feed_location", "comments", "total_count")
    return None


async def _filter_requests(route):
    req = route.request
    if req.resource_type in BLOCKED_RESOURCES or (req.is_navigation_request() and not host_in(req.url, NAV_HOSTS)):
        await route.abort()
    else:
        await route.continue_()


class BrowserPool:
    """One shared headless browser; each post gets its own short-lived context (a fresh guest identity)."""

    def __init__(self, max_pages=2, state_path=None):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_pages)
        self.channel = None
        self.state_path = state_path  # saved login session (storage_state); None or missing file = guest mode

    @property
    def has_session(self):
        return bool(self.state_path and os.path.exists(self.state_path))

    async def browser(self):
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser, self.channel = await launch_browser(self._pw, headless=True)
            return self._browser

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def open_post(self, url, feedback_id=None, cookie=None, proxy=None, timeout=45):
        """Open a post like a visitor; return templates + feedback id + title + total comment count."""
        target = normalize_target(url)
        if not host_in(target, FB_HOSTS):
            raise RuntimeError("Chỉ hỗ trợ link Facebook.")
        browser = await self.browser()
        async with self._sem:
            # a per-call cookie (one user's own session) wins; else the shared saved login; else guest
            storage = None if cookie else (self.state_path if self.has_session else None)
            ctx = await browser.new_context(locale="en-US", user_agent=chrome_ua(browser.version),
                                            viewport={"width": 1280, "height": 900}, proxy=playwright_proxy(proxy),
                                            storage_state=storage)
            try:
                if cookie:
                    await ctx.add_cookies(parse_cookie_string(cookie))
                await ctx.route("**/*", _filter_requests)
                page = await ctx.new_page()
                await page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
                info = None
                for _ in range(24):  # the comment modules load shortly after the page
                    if urlparse(page.url).path.startswith(("/login", "/checkpoint")):
                        raise LoginRequired("Bài viết yêu cầu đăng nhập (nhóm kín, bài riêng tư, hoặc Facebook "
                                            "bắt đăng nhập). Admin có thể thêm cookie tài khoản ở trang /admin.")
                    info = await page.evaluate(PAGE_JS, list(QUERIES))
                    if info and core.TOP_QUERY in info["mods"] and core.REPLY_QUERIES[0] in info["mods"]:
                        break
                    await page.wait_for_timeout(500)
                html = await page.content()
                title = await page.title()
                final_url = page.url
                user_agent = await page.evaluate("navigator.userAgent")
                cookie_header = "; ".join(f'{c["name"]}={c["value"]}'
                                          for c in await ctx.cookies("https://www.facebook.com"))
            finally:
                await ctx.close()

        if not info or not (info.get("lsd") or info.get("dtsg")):
            raise RuntimeError("Không đọc được dữ liệu trang. Link sai, bài đã xoá, hoặc Facebook đổi giao diện.")
        for name in QUERIES:  # fill gaps from earlier pages, remember what this page had
            if name in info["mods"]:
                _mods_cache[name] = info["mods"][name]
            elif name in _mods_cache:
                info["mods"][name] = _mods_cache[name]
        if core.TOP_QUERY not in info["mods"]:
            raise RuntimeError("Trang này không tải phần bình luận. Thử link dạng /posts/... của bài viết.")

        post_id = None
        if feedback_id:
            post_id = core.decode_feedback(feedback_id)
        else:
            feedback_id, post_id = core.pick_feedback(html, [url, final_url])
            if not feedback_id:
                raise RuntimeError("Không tìm thấy ID bài viết trong trang.")

        templates = build_templates(info, cookie_header, user_agent, final_url)
        total = await root_total(templates, feedback_id, proxy)
        if total is False:
            raise RuntimeError("Facebook không trả bình luận cho bài này (bài riêng tư, đã xoá, hoặc cần đăng nhập).")
        return {
            "templates": templates, "feedback_id": feedback_id, "post_id": post_id,
            "title": title.strip()[:150] or None, "final_url": final_url, "total_hint": total,
            "mode": "account" if info.get("user", "0") != "0" else "guest",
        }


def pick_proxy(proxies):
    return random.choice(proxies) if proxies else None


async def launch_browser(pw, headless=True):
    """Launch installed Chrome/Edge first, then Playwright's bundled Chromium."""
    args = ["--disable-dev-shm-usage"]           # avoid /dev/shm crashes in containers
    if os.environ.get("PLAYWRIGHT_NO_SANDBOX"):  # required when running as root in Docker
        args.append("--no-sandbox")
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            browser = await pw.chromium.launch(channel=channel, headless=headless, args=args)
            return browser, channel or "chromium"
        except Exception as e:
            errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:100]}")
    raise RuntimeError("Không mở được trình duyệt. Cài Google Chrome hoặc chạy "
                       "'python -m playwright install chromium'. " + " | ".join(errors))


# ---------------------------------------------------------------- login session

def _fb_cookies(cookies):
    return [c for c in cookies if any(c.get("domain", "").lstrip(".").endswith(h) for h in FB_HOSTS)]


def _uid_from_cookies(cookies):
    """The logged-in user id Facebook stores in the c_user cookie (not '0' = logged in)."""
    for c in cookies:
        if c.get("name") == "c_user" and str(c.get("value") or "0") != "0":
            return c["value"]
    return None


def account_id(state_path=STATE_PATH):
    """User id of the saved login session, or None if there is no usable session."""
    try:
        with open(state_path, encoding="utf-8") as f:
            return _uid_from_cookies(json.load(f).get("cookies", []))
    except (OSError, ValueError):
        return None


def clear_session(state_path=STATE_PATH):
    try:
        os.remove(state_path)
        return True
    except FileNotFoundError:
        return False


def save_session(data, state_path=STATE_PATH):
    """Validate a Playwright storage_state (dict or JSON text) and save it; return the user id.

    Lets the login be done on one machine (double-click the launcher -> fb_state.json) and the
    resulting file be uploaded to a headless server, so no GUI is ever needed on the server.
    """
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("File không đúng định dạng phiên đăng nhập.")
    cookies = _fb_cookies(data.get("cookies", []))
    uid = _uid_from_cookies(cookies)
    if not uid:
        raise ValueError("File không chứa đăng nhập Facebook (thiếu cookie c_user). "
                         "Hãy tạo bằng cách bấm đúp dang-nhap-facebook.bat trên máy của bạn.")
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "origins": data.get("origins", [])}, f)
    os.replace(tmp, state_path)
    return uid


# platform -> (cookies that must be present to count as logged in, cookie holding the account id)
PLATFORM_AUTH = {
    "facebook": (("c_user",), "c_user"),
    "threads": (("sessionid",), "ds_user_id"),
    "instagram": (("sessionid",), "ds_user_id"),
}


def normalize_user_session(raw, platform="facebook"):
    """Accept what a user pastes — a cookie string, a Cookie-Editor JSON export, or a storage_state JSON —
    and return (account_id, cookie_string). Raises ValueError if the required login cookies are missing."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Chưa dán cookie.")
    if raw[0] in "{[":
        data = json.loads(raw)
        items = data.get("cookies", []) if isinstance(data, dict) else data
        pairs = [(c.get("name"), c.get("value")) for c in items if isinstance(c, dict)]
    else:
        pairs = [p.strip().split("=", 1) for p in raw.split(";") if "=" in p]
    pairs = [(k, v) for k, v in pairs if k]
    names = {k for k, _ in pairs}
    need, id_cookie = PLATFORM_AUTH.get(platform, PLATFORM_AUTH["facebook"])
    missing = [n for n in need if n not in names]
    if missing:
        where = {"facebook": "facebook.com", "threads": "threads.com", "instagram": "instagram.com"}.get(platform, "")
        raise ValueError(f"Không thấy cookie đăng nhập ({', '.join(missing)}). "
                         f"Hãy đăng nhập rồi export cookie của {where}.")
    uid = next((v for k, v in pairs if k == id_cookie and v not in (None, "", "0")), None) or "?"
    return uid, "; ".join(f"{k}={v}" for k, v in pairs)


async def login(state_path=STATE_PATH, proxy=None, timeout=300, log=print):
    """Open a REAL browser window so the person signs in themselves, then save the session locally.

    The app never handles the password: Facebook does the login in its own window, and only the
    cookies it sets afterwards are written to `state_path` for later scraping. Headful on purpose,
    so the person can complete email/password and any 2FA. Returns the logged-in user id.
    """
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    async with async_playwright() as pw:
        browser, channel = await launch_browser(pw, headless=False)
        ctx = await browser.new_context(locale="en-US", viewport={"width": 1100, "height": 820},
                                        proxy=playwright_proxy(proxy))
        try:
            page = await ctx.new_page()
            await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=60000)
            log(f"Đã mở cửa sổ {channel}. Hãy đăng nhập Facebook trong cửa sổ đó (có thể mất tối đa {timeout}s)…")
            deadline = time.time() + timeout
            uid = None
            while time.time() < deadline:
                if not ctx.pages or all(p.is_closed() for p in ctx.pages):
                    break  # the person closed the window
                uid = _uid_from_cookies(await ctx.cookies("https://www.facebook.com"))
                if uid:
                    break
                await asyncio.sleep(1)
            if not uid:
                raise RuntimeError("Chưa đăng nhập xong (hết thời gian chờ hoặc cửa sổ đã bị đóng).")
            await asyncio.sleep(1.5)  # let the session cookies settle after redirect
            state = await ctx.storage_state()
            state["cookies"] = _fb_cookies(state.get("cookies", []))
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, state_path)
            log(f"Đã lưu phiên đăng nhập (tài khoản {uid}).")
            return uid
        finally:
            await browser.close()


def _cli():
    import argparse
    import sys
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Quản lý phiên đăng nhập Facebook cho scraper")
    ap.add_argument("cmd", choices=["login", "whoami", "logout"], help="login = mở cửa sổ để đăng nhập")
    ap.add_argument("--state", default=STATE_PATH, help="đường dẫn file lưu phiên")
    ap.add_argument("--timeout", type=int, default=300, help="số giây chờ đăng nhập")
    args = ap.parse_args()
    if args.cmd == "login":
        uid = asyncio.run(login(args.state, timeout=args.timeout))
        print(f"OK: đã đăng nhập tài khoản {uid}. Giờ chạy 'python app.py' là cào ở chế độ đã đăng nhập.")
    elif args.cmd == "whoami":
        uid = account_id(args.state)
        print(f"Đang đăng nhập tài khoản {uid}." if uid else "Chưa có phiên đăng nhập (đang ở chế độ khách).")
    else:
        print("Đã xoá phiên đăng nhập." if clear_session(args.state) else "Không có phiên để xoá.")


if __name__ == "__main__":
    _cli()
