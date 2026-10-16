"""
Threads (threads.com) reply scraper.

A post's replies load via the GraphQL query `BarcelonaPostPageDirectRepliesRefetchQuery`, paginated by an
`after` cursor. Unlike Facebook, Threads rate-limits (HTTP 429) requests replayed from httpx — but requests
made by the real page are fine. So we open the post once, capture that request as a template, then paginate
by calling fetch() *inside the page* (same origin, cookies and TLS fingerprint as the browser).

Reply path in the response:
  data.media.text_post_app_info.direct_replies.edges[].node.posts.edges[].node
Each posts.edges group is one reply thread: node[0] is a top-level reply, node[1:] are its nested replies.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx  # only for its HTTPError type (requests go through the browser)

import fb_browser  # reuse launch_browser, playwright_proxy, chrome_ua, cookie/host helpers
import scraper as core

HOSTS = ("threads.net", "threads.com")
REPLY_QUERY = "BarcelonaPostPageDirectRepliesRefetchQuery"
PAGE_SIZE = 25                 # `first`: the site defaults to 4; a bigger batch means fewer requests
BLOCKED = {"image", "media", "font"}

# Runs in the page: replay the reply query with the browser's own cookies/fingerprint (no 429).
FETCH_JS = """async ([url, body, headers]) => {
  try {
    const r = await fetch(url, {method: 'POST', headers, body, credentials: 'include'});
    return {status: r.status, text: await r.text()};
  } catch (e) { return {status: -1, text: String(e)}; }
}"""


# -------------------------------------------------------------- response parsing

def _dig(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _reply_text(media):
    cap = _dig(media, "caption", "text")
    if cap:
        return cap
    frags = _dig(media, "text_post_app_info", "text_fragments", "fragments") or []
    return "".join(f.get("plaintext", "") for f in frags if isinstance(f, dict)) or None


def _to_record(media, parent_id, depth):
    user = media.get("user") or {}
    tpa = media.get("text_post_app_info") or {}
    ts = media.get("taken_at")
    created = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else None
    username, code = user.get("username"), media.get("code")
    return {
        "id": str(media["pk"]) if media.get("pk") is not None else None,
        "parent_id": parent_id,
        "depth": depth,
        "author_id": user.get("pk") or user.get("id"),
        "author_name": username,
        "text": _reply_text(media),
        "created_time": created,
        "reactions": media.get("like_count"),
        "reply_count": tpa.get("direct_reply_count"),
        "url": f"https://www.threads.com/@{username}/post/{code}" if username and code else None,
    }


def find_direct_replies(obj):
    """Locate the direct_replies connection ({edges, page_info}) anywhere in a response chunk."""
    if isinstance(obj, dict):
        dr = _dig(obj, "text_post_app_info", "direct_replies")
        if isinstance(dr, dict) and "edges" in dr:
            return dr
        if isinstance(obj.get("edges"), list) and isinstance(obj.get("page_info"), dict) \
                and obj["edges"] and "posts" in (obj["edges"][0].get("node") or {}):
            return obj
        for v in obj.values():
            r = find_direct_replies(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_direct_replies(v)
            if r:
                return r
    return None


def _connection(chunks):
    for ch in chunks:
        conn = find_direct_replies(ch.get("data") if isinstance(ch.get("data"), dict) else ch)
        if conn:
            return conn
    return None


def parse_page(chunks):
    """Root replies: group[0] is a top-level reply (depth 0), group[1:] are its nested replies (depth 1)."""
    conn = _connection(chunks)
    if not conn:
        return [], False, None
    records = []
    for edge in conn.get("edges") or []:
        group = ((edge.get("node") or {}).get("posts") or {}).get("edges") or []
        top_id = None
        for i, pe in enumerate(group):
            media = pe.get("node") or {}
            if not media.get("pk"):
                continue
            if i == 0:
                rec = _to_record(media, None, 0)
                top_id = rec["id"]
            else:
                rec = _to_record(media, top_id, 1)
            records.append(rec)
    pi = conn.get("page_info") or {}
    return records, bool(pi.get("has_next_page")), pi.get("end_cursor")


def parse_children(chunks, parent_id, depth):
    """A reply's own thread (postID = that reply's pk): take each group's direct child under `parent_id`.

    Grandchildren (group[1:]) are left for when that child is expanded, so each node lands under its real parent.
    """
    conn = _connection(chunks)
    if not conn:
        return [], False, None
    records = []
    for edge in conn.get("edges") or []:
        group = ((edge.get("node") or {}).get("posts") or {}).get("edges") or []
        if group and (group[0].get("node") or {}).get("pk"):
            records.append(_to_record(group[0]["node"], parent_id, depth))
    pi = conn.get("page_info") or {}
    return records, bool(pi.get("has_next_page")), pi.get("end_cursor")


# -------------------------------------------------------------- id / title helpers

ROOT_ID_RES = [re.compile(p) for p in (r'"post_id":"(\d{10,})"', r'"media_id":"(\d{10,})"')]
CODE_RE = re.compile(r"/post/([A-Za-z0-9_-]+)")


def code_from_url(url):
    m = CODE_RE.search(url or "")
    return m.group(1) if m else None


def root_id_from_html(html, code):
    """The numeric pk of the post whose shortcode is `code` (the one we want replies for)."""
    if code:
        for m in re.finditer(r'"code":"' + re.escape(code) + r'"', html):
            pm = re.search(r'"pk":"(\d{10,})"', html[max(0, m.start() - 1500): m.end() + 1500])
            if pm:
                return pm.group(1)
    return next((m.group(1) for r in ROOT_ID_RES for m in [r.search(html)] if m), None)


def _dig_total(html, code):
    """Best-effort total direct reply count of the root post."""
    if code:
        for m in re.finditer(r'"code":"' + re.escape(code) + r'"', html):
            cm = re.search(r'"direct_reply_count":(\d+)', html[max(0, m.start() - 4000): m.end() + 4000])
            if cm:
                return int(cm.group(1))
    m = re.search(r'"direct_reply_count":(\d+)', html)
    return int(m.group(1)) if m else None


# -------------------------------------------------------------- browser session

async def _filter(route):
    if route.request.resource_type in BLOCKED:
        await route.abort()
    else:
        await route.continue_()


class ThreadsPool:
    """Shared headless browser; each scrape opens a post page and keeps it open for in-page fetches."""

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
        """Open the post, capture the reply request, and keep the page open for paginated in-page fetches."""
        target = re.sub(r"^https?://(?:www\.)?threads\.net", "https://www.threads.com", (url or "").strip())
        if not fb_browser.host_in(target, HOSTS):
            raise RuntimeError("Chỉ hỗ trợ link Threads.")
        code = code_from_url(target)
        browser = await self.browser()
        await self._sem.acquire()
        ctx = None
        try:
            ctx = await browser.new_context(locale="en-US", user_agent=fb_browser.chrome_ua(browser.version),
                                            viewport={"width": 1280, "height": 1000},
                                            proxy=fb_browser.playwright_proxy(proxy),
                                            storage_state=self.state_path if self.has_session else None)
            if cookie:
                await ctx.add_cookies(fb_browser.parse_cookie_string(cookie, ".threads.com"))
            await ctx.route("**/*", _filter)
            page = await ctx.new_page()
            cap = {}

            async def on_req(req):
                if cap or req.method != "POST" or "/graphql" not in req.url:
                    return
                form = dict(parse_qsl(req.post_data or "", keep_blank_values=True))
                if form.get("fb_api_req_friendly_name") == REPLY_QUERY:
                    cap.update(url=req.url, form=form, headers=await req.all_headers())

            page.on("request", lambda r: asyncio.ensure_future(on_req(r)))
            await page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
            await page.wait_for_timeout(4000)
            title = (await page.title() or "").strip()[:150] or None
            html = await page.content()
            deadline = time.time() + timeout
            while not cap and time.time() < deadline:
                if urlparse(page.url).path.startswith(("/login", "/accounts/login")):
                    raise RuntimeError("Threads chuyển sang trang đăng nhập. Thêm cookie Instagram để cào bài này.")
                await page.keyboard.press("Escape")
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1200)
            if not cap:
                raise RuntimeError("Không bắt được dữ liệu reply của Threads. Bài có thể không có trả lời, "
                                   "hoặc Threads đổi giao diện.")
            variables = json.loads(cap["form"].get("variables", "{}"))
            post_id = variables.get("postID") or root_id_from_html(html, code)
            headers = {k: v for k, v in cap["headers"].items() if k.startswith("x-") or k == "content-type"}
            info = {"feedback_id": post_id, "post_id": post_id, "title": title, "final_url": target,
                    "total_hint": _dig_total(html, code),
                    "mode": "account" if (self.has_session or cookie) else "guest"}
            return ThreadsSession(self._sem, ctx, page, cap["url"], cap["form"], variables, headers, info)
        except BaseException:
            if ctx is not None:
                await ctx.close()
            self._sem.release()
            raise


class ThreadsSession:
    """A live Threads post page; paginate replies through fetch() running in the page's own context."""

    def __init__(self, sem, ctx, page, url, form, variables, headers, info):
        self._sem = sem
        self._ctx = ctx
        self._page = page
        self.url = url
        self.form = form
        self.variables = variables
        self.headers = headers
        self.info = info
        self._closed = False

    async def fetch(self, variables):
        form = dict(self.form)
        form["variables"] = json.dumps(variables, separators=(",", ":"))
        res = await self._page.evaluate(FETCH_JS, [self.url, urlencode(form), self.headers])
        if res.get("status") != 200:
            raise httpx.HTTPError(f"HTTP {res.get('status')}: {str(res.get('text', ''))[:160]}")
        chunks = core.parse_chunks(res["text"])
        if not chunks:
            raise httpx.HTTPError("empty response")
        return chunks

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            await self._ctx.close()
        finally:
            self._sem.release()


# -------------------------------------------------------------- scraper

MAX_NESTED = 5000   # safety cap on how many reply-threads we expand per post
MAX_DEPTH = 2       # expand down to reply-of-reply; deeper is left as-is


class ThreadsScraper:
    """Scrape a post's replies, then expand each reply thread (postID = reply pk) to fetch nested replies
    the root response doesn't include. Writes the same comments.jsonl as Facebook."""

    def __init__(self, session, out_dir, delay=0.8, retries=5, log=print, log_progress=True, **_ignored):
        import os
        from collections import deque
        self.session = session
        self.post_id = session.info.get("post_id")
        self.delay = delay
        self.retries = retries
        self.log = log
        self.log_progress = log_progress
        os.makedirs(out_dir, exist_ok=True)
        self.jsonl_path = os.path.join(out_dir, "comments.jsonl")
        self.state_path = os.path.join(out_dir, "state.json")
        self.seen = set()
        self.root_cursor = None
        self.root_done = False
        self.queue = deque()        # reply threads still to expand: {"pk","depth"}
        self.queued = set()         # pks ever queued or expanded (so we never re-queue)
        self.pages = 0              # root pages
        self.nested_pages = 0       # expansion requests
        self.requests = 0
        self.reply_failed = 0
        self.t0 = time.time()
        self._load_state()
        self.out = open(self.jsonl_path, "a", encoding="utf-8")

    # fields the web UI reads off a scraper
    @property
    def top_pages(self):
        return self.pages

    @property
    def top_done(self):
        return self.root_done

    @property
    def reply_pending(self):
        return len(self.queue)

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
                self.root_cursor = st.get("root_cursor", st.get("cursor"))
                self.root_done = st.get("root_done", st.get("done", False))
                self.queue.extend(st.get("queue") or [])
                self.queued = {j["pk"] for j in self.queue} | set(st.get("expanded") or [])
                self.log(f"[resume] {len(self.seen)} reply | {len(self.queue)} chuỗi lồng còn mở rộng")
            except (OSError, ValueError):
                pass

    def _save_state(self):
        import os
        expanded = sorted(self.queued - {j["pk"] for j in self.queue})
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"root_cursor": self.root_cursor, "root_done": self.root_done,
                       "queue": list(self.queue), "expanded": expanded, "post_id": self.post_id}, f)
        os.replace(tmp, self.state_path)

    def _emit(self, records):
        for rec in records:
            if rec["id"] and rec["id"] not in self.seen:
                self.seen.add(rec["id"])
                self.out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # a reply that declares its own replies, not yet deep enough, gets its thread expanded
            if (rec["id"] and (rec.get("reply_count") or 0) > 0 and (rec.get("depth") or 0) < MAX_DEPTH
                    and rec["id"] not in self.queued and len(self.queued) < MAX_NESTED):
                self.queued.add(rec["id"])
                self.queue.append({"pk": rec["id"], "depth": rec.get("depth") or 0})

    async def _fetch(self, variables):
        import random
        delay = 2
        for attempt in range(self.retries):
            try:
                chunks = await self.session.fetch(variables)
                self.requests += 1
                return chunks
            except httpx.HTTPError as e:
                if attempt == self.retries - 1:
                    raise
                wait = delay * (2 ** attempt) + random.random()
                self.log(f"  ! {e!s:.140} -> thử lại sau {wait:.1f}s")
                await asyncio.sleep(wait)

    async def _expand(self, job):
        """Paginate one reply's own thread; its direct children land under it (parent = job pk)."""
        v = dict(self.session.variables)
        v["postID"] = job["pk"]
        if "first" in v:
            v["first"] = PAGE_SIZE
        cursor, seen_cursors = None, set()
        while True:
            v["after"] = cursor
            try:
                chunks = await self._fetch(v)
            except httpx.HTTPError as e:
                self.reply_failed += 1
                self.log(f"  ! mở rộng {job['pk']} lỗi: {e!s:.120}")
                return
            records, has_next, cursor = parse_children(chunks, job["pk"], (job["depth"] or 0) + 1)
            self._emit(records)
            self.nested_pages += 1
            if not has_next or not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            if self.delay:
                await asyncio.sleep(self.delay)

    async def run(self):
        base = dict(self.session.variables)
        if self.post_id:
            base["postID"] = self.post_id
        if "first" in base:
            base["first"] = PAGE_SIZE
        try:
            while not self.root_done:                       # phase 1: the post's direct replies
                base["after"] = self.root_cursor
                chunks = await self._fetch(base)
                records, has_next, end_cursor = parse_page(chunks)
                self._emit(records)
                self.pages += 1
                if not has_next or not end_cursor or end_cursor == self.root_cursor:
                    self.root_done = True
                self.root_cursor = end_cursor or self.root_cursor
                if self.pages % 3 == 0:
                    self.out.flush()
                    self._save_state()
                if self.log_progress and self.pages % 5 == 0:
                    self.log(f"[{time.time()-self.t0:.0f}s] reply={len(self.seen)} trang={self.pages}")
                if self.delay:
                    await asyncio.sleep(self.delay)

            while self.queue:                               # phase 2: expand each reply's nested thread
                await self._expand(self.queue.popleft())
                if self.nested_pages % 5 == 0:
                    self.out.flush()
                    self._save_state()
                    if self.log_progress:
                        self.log(f"[{time.time()-self.t0:.0f}s] reply={len(self.seen)} | còn {len(self.queue)} chuỗi lồng")
        finally:
            self.out.flush()
            self._save_state()
            self.out.close()
            await self.session.close()
        self.log(f"[done] {len(self.seen)} reply trong {time.time()-self.t0:.0f}s "
                 f"({self.pages} trang gốc + {self.nested_pages} lượt mở rộng)")
