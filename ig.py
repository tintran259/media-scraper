"""
Instagram (instagram.com) comment scraper.

Instagram needs login to read comments, and comments load through its private REST endpoint
  GET /api/v1/media/{media_id}/comments/?can_support_threading=true&permalink_enabled=false[&max_id=<cursor>]
(not GraphQL). httpx replays get blocked, so we open the post in a headless browser with the user's
cookie, read the media id + csrf token, and call that endpoint with fetch() inside the page (same cookies
and TLS fingerprint). Pagination is by `next_max_id`.

Comment JSON: {"comments":[{pk, text, created_at, comment_like_count, child_comment_count,
  user:{username, pk}, ...}], "next_max_id": "...", "has_more_comments": bool, "comment_count": N}

NOTE: built from Instagram's known REST API + the logged-out page structure; validated offline. The live
flow needs an Instagram login to confirm. Top-level comments first; replies are a follow-up.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx  # for its HTTPError type

import fb_browser
import scraper as core

HOSTS = ("instagram.com", "instagr.am")
IG_APP_ID = "238260118697367"          # Instagram web app id (seen in its own requests)
BLOCKED = {"image", "media", "font"}

FETCH_JS = """async ([url, headers]) => {
  try {
    const r = await fetch(url, {method: 'GET', headers, credentials: 'include'});
    return {status: r.status, text: await r.text()};
  } catch (e) { return {status: -1, text: String(e)}; }
}"""


# -------------------------------------------------------------- response parsing

def _record(c, parent_id=None, depth=0):
    user = c.get("user") or {}
    ts = c.get("created_at")
    created = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else None
    pid = c.get("parent_comment_id") or parent_id
    return {
        "id": str(c.get("pk") or c.get("id")) if (c.get("pk") or c.get("id")) else None,
        "parent_id": str(pid) if pid else None,
        "depth": 1 if pid else depth,
        "author_id": user.get("pk") or user.get("id"),
        "author_name": user.get("username"),
        "text": c.get("text"),
        "created_time": created,
        "reactions": c.get("comment_like_count"),
        "reply_count": c.get("child_comment_count"),
        "url": None,
    }


def _find_connection(obj):
    """Fallback: a GraphQL comments connection ({edges, page_info}) if IG ever returns that shape."""
    if isinstance(obj, dict):
        if isinstance(obj.get("comments_connection"), dict) and "edges" in obj["comments_connection"]:
            return obj["comments_connection"]
        if isinstance(obj.get("edges"), list) and obj["edges"]:
            n = (obj["edges"][0] or {}).get("node") or {}
            if n.get("__typename") == "XIGComment" or "comment_like_count" in n:
                return obj
        for v in obj.values():
            r = _find_connection(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_connection(v)
            if r:
                return r
    return None


def parse_page(data):
    """Return (records, has_next, next_cursor) from one Instagram comments response (REST dict or chunks)."""
    if isinstance(data, list):                      # core.parse_chunks output
        data = data[0] if data else {}
    if isinstance(data, dict) and isinstance(data.get("comments"), list):   # REST shape
        records = [_record(c) for c in data["comments"] if c.get("pk") or c.get("id")]
        # ranked view: more comments load via next_min_id ("headload"); plain view uses next_max_id
        if data.get("next_min_id") and data.get("has_more_headload_comments", True):
            return records, True, ["min_id", data["next_min_id"]]
        if data.get("next_max_id") and data.get("has_more_comments", True):
            return records, True, ["max_id", data["next_max_id"]]
        return records, False, None
    conn = _find_connection(data)                   # GraphQL fallback
    if not conn:
        return [], False, None
    records = [_record((e.get("node") or {})) for e in (conn.get("edges") or []) if (e.get("node") or {}).get("pk")]
    pi = conn.get("page_info") or {}
    return records, bool(pi.get("has_next_page")), pi.get("end_cursor")


# -------------------------------------------------------------- id / title helpers

CODE_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
MEDIA_ID_RES = [re.compile(p) for p in (r'"media_id":"(\d+)"', r'"id":"POLARIS_(\d+)"',
                                        r'\\"media_id\\":\\"(\d+)\\"', r'"pk":"(\d{10,})","code"')]
COUNT_RE = re.compile(r'"comment_count":(\d+)')
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


def code_from_url(url):
    m = CODE_RE.search(url or "")
    return m.group(1) if m else None


SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_media_id(code):
    """Instagram's /p/<code>/ shortcode decodes to the numeric media id (base64 over a custom alphabet)."""
    if not code:
        return None
    n = 0
    for ch in code:
        i = SHORTCODE_ALPHABET.find(ch)
        if i < 0:
            return None
        n = n * 64 + i
    return str(n) if n else None


def media_id_from_html(html):
    return next((m.group(1) for r in MEDIA_ID_RES for m in [r.search(html)] if m), None)


def total_from_html(html):
    m = COUNT_RE.search(html)
    return int(m.group(1)) if m else None


def comments_url(media_id, cursor=None):
    """cursor is [param, value] — IG paginates with either min_id (ranked/headload) or max_id (plain)."""
    from urllib.parse import quote
    url = f"https://www.instagram.com/api/v1/media/{media_id}/comments/?can_support_threading=true&permalink_enabled=false"
    if cursor:
        url += f"&{cursor[0]}={quote(str(cursor[1]), safe='')}"
    return url


def child_comments_url(media_id, comment_id, cursor=None):
    from urllib.parse import quote
    url = f"https://www.instagram.com/api/v1/media/{media_id}/comments/{comment_id}/child_comments/?"
    if cursor:
        url += f"{cursor[0]}={quote(str(cursor[1]), safe='')}"
    return url


def parse_children(data, parent_id):
    """A comment's replies (child_comments): records under `parent_id`, plus (has_next, cursor)."""
    if isinstance(data, list):
        data = data[0] if data else {}
    kids = (data.get("child_comments") or data.get("comments") or []) if isinstance(data, dict) else []
    records = [_record(c, parent_id=parent_id, depth=1) for c in kids if (c.get("pk") or c.get("id"))]
    nxt = (data.get("next_max_id") or data.get("next_min_id")) if isinstance(data, dict) else None
    has_more = data.get("has_more_tail_child_comments") if isinstance(data, dict) else None
    if not nxt:
        return records, False, None
    param = "max_id" if data.get("next_max_id") else "min_id"
    return records, bool(has_more) if has_more is not None else True, [param, nxt]


# -------------------------------------------------------------- browser session

async def _filter(route):
    if route.request.resource_type in BLOCKED:
        await route.abort()
    else:
        await route.continue_()


class IgPool:
    """Shared headless browser; each scrape opens a post (logged in) and keeps the page for in-page fetches."""

    def __init__(self, max_pages=2, state_path=None):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_pages)
        self.channel = None
        self.state_path = state_path

    @property
    def has_session(self):
        import os
        return bool(self.state_path and os.path.exists(self.state_path))

    async def browser(self):
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    from playwright.async_api import async_playwright
                    self._pw = await async_playwright().start()
                self._browser, self.channel = await fb_browser.launch_browser(self._pw, headless=True)
            return self._browser

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def new_session(self, url, cookie=None, proxy=None, timeout=45):
        target = re.sub(r"^https?://(?:www\.)?instagr\.am", "https://www.instagram.com", (url or "").strip())
        if not fb_browser.host_in(target, HOSTS):
            raise RuntimeError("Chỉ hỗ trợ link Instagram.")
        if not (cookie or self.has_session):
            raise RuntimeError("Instagram cần đăng nhập. Hãy thêm cookie Instagram (có sessionid) ở tab Instagram.")
        browser = await self.browser()
        await self._sem.acquire()
        ctx = None
        try:
            ctx = await browser.new_context(locale="en-US", user_agent=fb_browser.chrome_ua(browser.version),
                                            viewport={"width": 1280, "height": 1000},
                                            proxy=fb_browser.playwright_proxy(proxy),
                                            storage_state=self.state_path if self.has_session else None)
            if cookie:
                await ctx.add_cookies(fb_browser.parse_cookie_string(cookie, ".instagram.com"))
            await ctx.route("**/*", _filter)
            page = await ctx.new_page()
            await page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
            await page.wait_for_timeout(4000)
            if urlparse(page.url).path.startswith("/accounts/login"):
                raise RuntimeError("Instagram yêu cầu đăng nhập (cookie sai hoặc hết hạn).")
            html = await page.content()
            title = (await page.title() or "").strip()[:150] or None
            cookies = await ctx.cookies("https://www.instagram.com")
            csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken"), None)
            logged_in = any(c["name"] == "sessionid" and c["value"] for c in cookies)
            media_id = shortcode_to_media_id(code_from_url(target)) or media_id_from_html(html)
            if not media_id:
                raise RuntimeError("Không tìm thấy media id. Link phải dạng instagram.com/p/<code>/ "
                                   "hoặc instagram.com/reel/<code>/.")
            headers = {"x-ig-app-id": IG_APP_ID, "x-requested-with": "XMLHttpRequest",
                       "accept": "*/*"}
            if csrf:
                headers["x-csrftoken"] = csrf
            info = {"feedback_id": media_id, "post_id": media_id, "title": title, "final_url": target,
                    "total_hint": total_from_html(html), "mode": "account" if logged_in else "guest"}
            return IgSession(self._sem, ctx, page, media_id, headers, info)
        except BaseException:
            if ctx is not None:
                await ctx.close()
            self._sem.release()
            raise


class IgSession:
    def __init__(self, sem, ctx, page, media_id, headers, info):
        self._sem = sem
        self._ctx = ctx
        self._page = page
        self.media_id = media_id
        self.headers = headers
        self.info = info
        self._closed = False

    async def _get_json(self, url, what):
        res = await self._page.evaluate(FETCH_JS, [url, self.headers])
        text = str(res.get("text", ""))
        if res.get("status") != 200:
            raise httpx.HTTPError(f"HTTP {res.get('status')}: {text[:200]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            hint = "trang đăng nhập (cookie chưa nhận)" if text.lstrip().startswith("<") else text[:160]
            raise httpx.HTTPError(f"Instagram không trả JSON ({what}): " + hint)

    async def fetch(self, cursor=None):
        return await self._get_json(comments_url(self.media_id, cursor), "bình luận")

    async def fetch_children(self, comment_id, cursor=None):
        return await self._get_json(child_comments_url(self.media_id, comment_id, cursor), "reply")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            await self._ctx.close()
        finally:
            self._sem.release()


# -------------------------------------------------------------- scraper

class IgScraper:
    """Paginate a post's top-level comments via the REST endpoint (in-page fetch); write comments.jsonl."""

    def __init__(self, session, out_dir, delay=0.8, retries=5, log=print, log_progress=True, debug_dir=None, **_ignored):
        import os
        from collections import deque
        self.session = session
        self.debug_dir = debug_dir
        self.delay = delay
        self.retries = retries
        self.log = log
        self.log_progress = log_progress
        os.makedirs(out_dir, exist_ok=True)
        self.jsonl_path = os.path.join(out_dir, "comments.jsonl")
        self.state_path = os.path.join(out_dir, "state.json")
        self.seen = set()
        self.cursor = None
        self.done = False               # top-level comments done
        self.reply_queue = deque()      # parent comment ids whose replies to fetch
        self.reply_queued = set()       # queued or already expanded
        self.pages = 0
        self.reply_pages = 0
        self.requests = 0
        self.reply_failed = 0
        self.t0 = time.time()
        self._load_state()
        self.out = open(self.jsonl_path, "a", encoding="utf-8")

    @property
    def top_pages(self):
        return self.pages

    @property
    def top_done(self):
        return self.done

    @property
    def reply_pending(self):
        return len(self.reply_queue)

    meta = {}
    streams = {}

    def _load_state(self):
        import os
        if os.path.exists(self.jsonl_path):
            for rec in core.iter_jsonl(self.jsonl_path):
                if rec.get("id"):
                    self.seen.add(rec["id"])
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    st = json.load(f)
                self.cursor, self.done = st.get("cursor"), st.get("done", False)
                self.reply_queue.extend(st.get("reply_queue") or [])
                self.reply_queued = set(self.reply_queue) | set(st.get("reply_done") or [])
                self.log(f"[resume] {len(self.seen)} bình luận | {len(self.reply_queue)} comment chờ lấy reply")
            except (OSError, ValueError):
                pass

    def _save_state(self):
        import os
        done_ids = sorted(self.reply_queued - set(self.reply_queue))
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cursor": self.cursor, "done": self.done,
                       "reply_queue": list(self.reply_queue), "reply_done": done_ids}, f)
        os.replace(tmp, self.state_path)

    def _emit(self, records):
        for rec in records:
            if rec["id"] and rec["id"] not in self.seen:
                self.seen.add(rec["id"])
                self.out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # a top-level comment that has replies gets its child-comment thread fetched
            if (rec["id"] and (rec.get("depth") or 0) == 0 and (rec.get("reply_count") or 0) > 0
                    and rec["id"] not in self.reply_queued):
                self.reply_queued.add(rec["id"])
                self.reply_queue.append(rec["id"])

    def _diagnose(self, data):
        """Log the first response's shape so pagination fields are visible if something is off."""
        if not isinstance(data, dict):
            self.log(f"[debug] response không phải JSON object: {str(data)[:120]}")
            return
        pag = {k: data.get(k) for k in ("has_more_comments", "has_more_headload_comments", "next_max_id",
                                        "next_min_id", "comment_count") if k in data}
        self.log(f"[debug] IG keys={sorted(data)[:25]} | pagination={pag} | comments={len(data.get('comments', []))}")
        if self.debug_dir:
            import os
            os.makedirs(self.debug_dir, exist_ok=True)
            diag = {"top_level_keys": sorted(data), "num_comments": len(data.get("comments", [])), "pagination": pag}
            with open(os.path.join(self.debug_dir, "ig_page1.json"), "w", encoding="utf-8") as f:
                json.dump(diag, f, ensure_ascii=False, indent=1)

    async def _retry(self, coro_fn):
        import random
        delay = 2
        for attempt in range(self.retries):
            try:
                data = await coro_fn()
                self.requests += 1
                return data
            except httpx.HTTPError as e:
                if attempt == self.retries - 1:
                    raise
                wait = delay * (2 ** attempt) + random.random()
                self.log(f"  ! {e!s:.160} -> thử lại sau {wait:.1f}s")
                await asyncio.sleep(wait)

    async def _fetch(self, cursor):
        return await self._retry(lambda: self.session.fetch(cursor))

    async def _fetch_children(self, comment_id, cursor):
        return await self._retry(lambda: self.session.fetch_children(comment_id, cursor))

    async def run(self):
        try:
            while not self.done:                        # phase 1: top-level comments
                data = await self._fetch(self.cursor)
                if self.pages == 0:
                    self._diagnose(data)
                records, has_next, next_cursor = parse_page(data)
                self._emit(records)
                self.pages += 1
                if not has_next or not next_cursor or next_cursor == self.cursor:
                    self.done = True
                self.cursor = next_cursor or self.cursor
                if self.pages % 3 == 0:
                    self.out.flush()
                    self._save_state()
                if self.log_progress and self.pages % 5 == 0:
                    self.log(f"[{time.time()-self.t0:.0f}s] bình luận={len(self.seen)} trang={self.pages}")
                if self.delay:
                    await asyncio.sleep(self.delay)

            while self.reply_queue:                      # phase 2: replies (child comments)
                await self._expand_replies(self.reply_queue.popleft())
                if self.reply_pages % 5 == 0:
                    self.out.flush()
                    self._save_state()
                    if self.log_progress:
                        self.log(f"[{time.time()-self.t0:.0f}s] bình luận={len(self.seen)} | còn {len(self.reply_queue)} comment lấy reply")
        finally:
            self.out.flush()
            self._save_state()
            self.out.close()
            await self.session.close()
        self.log(f"[done] {len(self.seen)} bình luận trong {time.time()-self.t0:.0f}s "
                 f"({self.pages} trang gốc + {self.reply_pages} lượt reply)")

    async def _expand_replies(self, comment_id):
        cursor, seen_cursors = None, set()
        while True:
            try:
                data = await self._fetch_children(comment_id, cursor)
            except httpx.HTTPError as e:
                self.reply_failed += 1
                self.log(f"  ! reply của {comment_id} lỗi: {e!s:.120}")
                return
            if self.reply_pages == 0 and self.debug_dir and isinstance(data, dict):
                import os
                os.makedirs(self.debug_dir, exist_ok=True)
                with open(os.path.join(self.debug_dir, "ig_child1.json"), "w", encoding="utf-8") as f:
                    json.dump({"keys": sorted(data), "num": len(data.get("child_comments") or data.get("comments") or []),
                               "pag": {k: data.get(k) for k in ("has_more_tail_child_comments", "next_max_id", "next_min_id") if k in data}}, f, ensure_ascii=False, indent=1)
            records, has_next, next_cursor = parse_children(data, comment_id)
            self._emit(records)
            self.reply_pages += 1
            if not has_next or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            if self.delay:
                await asyncio.sleep(self.delay)
