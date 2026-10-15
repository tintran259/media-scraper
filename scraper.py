"""
Facebook comment scraper (comments + replies) via the internal GraphQL API.

Giao diện web:  python app.py   (hoặc chạy run.bat) -> dán danh sách link -> bấm chạy.

Cách dùng CLI:
  python scraper.py --url <link bài viết> --out out           # tự lấy session bằng Chrome ẩn (fb_browser.py)
  python scraper.py --har session.har --url <link> --out out  # hoặc dùng session từ file HAR

Kết quả: out/comments.jsonl (ghi dần, resume được), out/comments.csv khi xong.
"""

import argparse
import asyncio
import base64
import csv
import html as htmllib
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlparse

import httpx

GQL_URL = "https://www.facebook.com/api/graphql/"
TOP_QUERY = "CommentsListComponentsPaginationQuery"
REPLY_QUERIES = ("Depth1CommentsListPaginationQuery", "Depth2CommentsListPaginationQuery")
ALL_COMMENTS_INTENT = "RANKED_UNFILTERED_CHRONOLOGICAL_REPLIES_INTENT_V1"
NEWEST_INTENT = "REVERSE_CHRONOLOGICAL_UNFILTERED_INTENT_V1"
# Top-level comments are paged through several orderings at once: same wall time, and comments one
# ordering never reaches (Facebook ends some lists early) can still show up in another.
TOP_INTENTS = (ALL_COMMENTS_INTENT, NEWEST_INTENT)
INTENT_NAMES = {ALL_COMMENTS_INTENT: "all", NEWEST_INTENT: "newest"}
CONN_META_KEYS = ("total_count", "count", "page_size", "is_not_behind_the_fold", "filtering_footer_string")
PARENT_REF_KEYS = {"comment_direct_parent", "parent_comment"}  # point at an ancestor, not a new comment

DROP_HEADERS = {"content-length", "accept-encoding", "host", "connection"}
SESSION_ERROR_CODES = {1357001, 1357045}  # logged out / invalid fb_dtsg: retrying won't help


# ---------------------------------------------------------------- HAR loading

def templates_from_har(har):
    """Extract GraphQL request templates (headers + form + variables) from a parsed HAR."""
    templates = {}
    for entry in har["log"]["entries"]:
        req = entry["request"]
        if "/api/graphql" not in req["url"] or req["method"] != "POST":
            continue
        post = req.get("postData") or {}
        if post.get("params"):
            form = {p["name"]: p.get("value", "") for p in post["params"]}
        else:
            form = dict(parse_qsl(post.get("text", ""), keep_blank_values=True))
        name = form.get("fb_api_req_friendly_name", "")
        if not name or name in templates:
            continue

        headers = {}
        for h in req["headers"]:
            k = h["name"].lower()
            if k.startswith(":") or k in DROP_HEADERS:
                continue
            headers[k] = h["value"]
        if "cookie" not in headers and req.get("cookies"):
            headers["cookie"] = "; ".join(f'{c["name"]}={c["value"]}' for c in req["cookies"])

        templates[name] = {
            "headers": headers,
            "form": form,
            "variables": json.loads(form.get("variables", "{}")),
        }
    return templates


def load_templates(har_path):
    with open(har_path, encoding="utf-8-sig") as f:
        return templates_from_har(json.load(f))


# ------------------------------------------------------- link -> feedback id

FEEDBACK_PREFIX = "ZmVlZGJhY2s6"  # base64("feedback:")
FEEDBACK_ID_RE = re.compile(r'"id":"(ZmVlZGJhY2s6(?:[A-Za-z0-9+=]|\\/)+)"')
URL_ID_RES = [re.compile(p) for p in (
    r"story_fbid=(\d+)", r"/posts/(\d+)", r"/permalink/(\d+)", r"multi_permalinks=(\d+)",
    r"[?&]fbid=(\d+)", r"[?&]v=(\d+)", r"/videos/(?:[^/?]+/)?(\d+)", r"/reel/(\d+)", r"/photos/[^/?]+/(\d+)",
)]
HTML_POST_ID_RES = [re.compile(p) for p in (r'"post_id":"(\d+)"', r'"top_level_post_id":"(\d+)"')]
DTSG_RES = [re.compile(p) for p in (r'"DTSGInitialData",\[\],\{"token":"([^"]+)"',
                                    r'"dtsg":\{"token":"([^"]+)"', r'name="fb_dtsg" value="([^"]+)"')]
LSD_RES = [re.compile(p) for p in (r'"LSD",\[\],\{"token":"([^"]+)"', r'name="lsd" value="([^"]+)"')]
COMMENT_COUNT_RE = re.compile(r'"comments?(?:_count)?":\{"total_count":(\d+)')
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


def encode_feedback(post_id):
    return base64.b64encode(f"feedback:{post_id}".encode()).decode()


def decode_feedback(fid):
    try:
        raw = base64.b64decode(fid + "=" * (-len(fid) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return raw[len("feedback:"):] if raw.startswith("feedback:") else None


def first_match(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1)
    return None


def nav_headers(api_headers):
    """Browser-like headers for opening a page, reusing UA/cookie/client hints from the HAR."""
    h = {k: v for k, v in api_headers.items()
         if k in ("user-agent", "cookie", "accept-language") or k.startswith("sec-ch-")}
    h.update({
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none", "sec-fetch-user": "?1", "upgrade-insecure-requests": "1",
    })
    return h


def pick_feedback(page, urls):
    """Choose the post-level feedback id on a post page (comment feedbacks are feedback:<post>_<comment>)."""
    counts, by_target = Counter(), {}
    for m in FEEDBACK_ID_RE.finditer(page):
        fid = m.group(1).replace("\\/", "/")
        target = decode_feedback(fid)
        if target and target.isdigit():
            counts[fid] += 1
            by_target.setdefault(target, fid)
    candidates = [m.group(1) for u in urls for p in URL_ID_RES for m in p.finditer(u)]
    candidates += [m.group(1) for p in HTML_POST_ID_RES for m in p.finditer(page)]
    for c in candidates:
        if c in by_target:
            return by_target[c], c
    if counts:
        fid = counts.most_common(1)[0][0]
        return fid, decode_feedback(fid)
    if candidates:
        return encode_feedback(candidates[0]), candidates[0]
    return None, None


def comment_count_hint(page, feedback_id):
    """Best-effort total comment count (incl. replies) near the post's feedback object."""
    best = None
    for needle in {feedback_id, feedback_id.replace("/", "\\/")}:
        for m in re.finditer(re.escape(f'"id":"{needle}"'), page):
            window = page[max(0, m.start() - 3000): m.end() + 3000]
            for c in COMMENT_COUNT_RE.findall(window):
                best = max(best or 0, int(c))
    return best


async def resolve_target(templates, target, proxy=None):
    """Turn a post link / numeric post id / feedback id into the feedback id the comment query needs.

    Also returns fresh fb_dtsg/lsd tokens found on the page, so an old HAR keeps working.
    """
    target = target.strip()
    info = {"feedback_id": None, "post_id": None, "title": None, "total_hint": None,
            "fb_dtsg": None, "lsd": None, "final_url": None}
    home = "https://www.facebook.com/"
    if target.startswith(FEEDBACK_PREFIX):
        info.update(feedback_id=target, post_id=decode_feedback(target))
        url = home
    elif target.isdigit():
        info.update(feedback_id=encode_feedback(target), post_id=target)
        url = home + target
    else:
        url = target if "://" in target else "https://" + target
        url = re.sub(r"^https?://(?:m|mbasic|touch|web)\.facebook\.com", "https://www.facebook.com", url)

    async with httpx.AsyncClient(http2=True, proxy=proxy, timeout=30, follow_redirects=True) as client:
        r = await client.get(url, headers=nav_headers(templates[TOP_QUERY]["headers"]))
    page, final = r.text, str(r.url)
    info["final_url"] = final
    if urlparse(final).path.startswith(("/login", "/checkpoint")) or 'id="login_form"' in page:
        raise RuntimeError("Session hết hạn hoặc bị checkpoint: export HAR mới rồi nạp lại.")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} khi mở {url}")
    info["fb_dtsg"] = first_match(DTSG_RES, page)
    info["lsd"] = first_match(LSD_RES, page)

    if not info["feedback_id"]:
        fid, pid = pick_feedback(page, [target, final])
        if not fid:
            raise RuntimeError("Không tìm thấy ID bài viết trong trang. Thử dán post ID (số) hoặc feedback ID.")
        info.update(feedback_id=fid, post_id=pid)
    if url != home:
        m = TITLE_RE.search(page)
        info["title"] = htmllib.unescape(m.group(1)).strip()[:150] if m else None
        info["total_hint"] = comment_count_hint(page, info["feedback_id"])
    return info


def apply_tokens(templates, fb_dtsg=None, lsd=None, referer=None):
    """Refresh per-session tokens in every template (jazoest is derived from fb_dtsg)."""
    for t in templates.values():
        form, headers = t["form"], t["headers"]
        if fb_dtsg and "fb_dtsg" in form:
            form["fb_dtsg"] = fb_dtsg
            if "jazoest" in form:
                form["jazoest"] = "2" + str(sum(map(ord, fb_dtsg)))
        if lsd:
            if "lsd" in form:
                form["lsd"] = lsd
            if "x-fb-lsd" in headers:
                headers["x-fb-lsd"] = lsd
        if referer and "referer" in headers:
            headers["referer"] = referer


# ------------------------------------------------------------ response parsing

def parse_chunks(text):
    """FB may return several JSON objects separated by newlines (@defer/@stream)."""
    if text.startswith("for (;;);"):
        text = text[9:]
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def is_comment(d):
    if d.get("__typename") == "Comment" and ("body" in d or "author" in d):
        return True
    return "legacy_fbid" in d and "author" in d and "body" in d


def dig(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def parse_count(value):
    """Exact ints, or the reduced strings ('1.5K') Facebook shows logged-out viewers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"([\d.]+)\s*([KkMm]?)", str(value).replace(",", "").strip())
    try:
        return int(float(m.group(1)) * {"": 1, "K": 1_000, "M": 1_000_000}[m.group(2).upper()]) if m else None
    except ValueError:
        return None


def comment_fbid(node):
    """legacy id of a Comment object or reference ({"id": base64("comment:<post>_<comment>")})."""
    if not isinstance(node, dict):
        return None
    if node.get("legacy_fbid"):
        return str(node["legacy_fbid"])
    gid = node.get("id") or ""
    try:
        raw = base64.b64decode(gid + "=" * (-len(gid) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return raw.rsplit("_", 1)[-1] if raw.startswith("comment:") else None


def walk(obj, on_comment, on_page_info, parent=None, depth=0):
    """Find every Comment node (with its nearest Comment ancestor) and every page_info."""
    if isinstance(obj, dict):
        if isinstance(obj.get("page_info"), dict) and "edges" in obj:
            on_page_info(obj["page_info"], depth, obj)
        elif "has_next_page" in obj or "has_previous_page" in obj:
            on_page_info(obj, depth, None)  # page_info on its own, e.g. in a deferred chunk
        if is_comment(obj):
            on_comment(obj, parent)
            parent = obj
        for k, v in obj.items():
            if k not in PARENT_REF_KEYS:
                walk(v, on_comment, on_page_info, parent, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, on_comment, on_page_info, parent, depth + 1)


def find_key(obj, key, max_depth=4):
    if max_depth < 0:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for k, v in obj.items():
            if "replies_connection" in k or k == "edges":
                continue
            r = find_key(v, key, max_depth - 1)
            if r is not None:
                return r
    return None


def to_record(node, parent_id):
    fb = node.get("feedback") or {}
    replies = fb.get("replies_fields") or {}
    reply_count = first(replies.get("count"), fb.get("total_reply_count"))
    reactions = parse_count(first(dig(fb, "reactors", "count"), dig(fb, "reactors", "count_reduced"),
                                  dig(fb, "unified_reactors", "count")))
    ts = node.get("created_time")
    created = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else ts
    author = node.get("author") or {}
    return {
        "id": first(node.get("legacy_fbid"), node.get("id")),
        "graphql_id": node.get("id"),
        "parent_id": parent_id,
        "depth": node.get("depth"),
        "author_id": author.get("id"),
        "author_name": author.get("name"),
        "author_url": author.get("url"),
        "text": dig(node, "body", "text"),
        "created_time": created,
        "reactions": reactions,
        "reply_count": reply_count,                               # replies this viewer can see
        "reply_total": first(replies.get("total_count"), reply_count),  # what Facebook counts, incl. hidden
        "url": first(fb.get("url"), node.get("url")),
        # internal, used to fetch replies
        "_feedback_id": fb.get("id"),
        "_expansion_token": first(dig(fb, "expansion_info", "expansion_token"),
                                  find_key(fb, "expansion_token")),
    }


def page_from_chunks(chunks, parent_hint):
    """Return (records, page_info, connection) for one GraphQL response; the last two may be None."""
    records, infos, errors = [], [], []

    def on_comment(node, parent):
        pid = comment_fbid(node.get("comment_direct_parent"))  # replies of replies arrive in the same list
        if pid is None:
            if parent is not None:
                pid = comment_fbid(parent)
            elif node.get("depth") != 0:
                pid = parent_hint
        records.append(to_record(node, pid))

    for ch in chunks:
        if ch.get("errors"):
            errors.extend(ch["errors"])
        base = len(ch.get("path") or [])  # deferred chunks are positioned by "path"
        walk(ch.get("data"), on_comment, lambda pi, d, conn: infos.append((d + base, pi, conn)))

    if errors and not records:
        raise FBError(errors)
    if not infos:
        return records, None, None
    _, pi, conn = min(infos, key=lambda x: x[0])
    return records, pi, conn


class FBError(Exception):
    def __init__(self, errors):
        self.errors = errors
        self.codes = {e.get("code") for e in errors if isinstance(e, dict)}
        super().__init__(json.dumps(errors, ensure_ascii=False)[:500])


# -------------------------------------------------------------------- scraper

def set_cursor(variables, cursor, direction="after"):
    """Point the query at `cursor` in one direction and clear the other cursor variables."""
    v = dict(variables)
    for k in list(v):
        lk = k.lower()
        if "cursor" in lk:
            v[k] = cursor if direction in lk or lk == "cursor" else None
    return v


REPLY_BATCH = -1  # like the top-level commentsAfterCount=-1; with Count=None FB returns only a ~6-reply preview


def reply_page_vars(variables, cursor, direction):
    """Cursor + Count for one reply page. The Count is the fix for truncated threads: without it
    Facebook returns only the small expansion preview and (wrongly) says has_next_page=false."""
    v = dict(variables)
    for k in list(v):
        lk = k.lower()
        if lk.endswith("aftercursor"):
            v[k] = cursor if direction == "after" else None
        elif lk.endswith("beforecursor"):
            v[k] = cursor if direction == "before" else None
        elif lk.endswith("aftercount"):
            v[k] = REPLY_BATCH if direction == "after" else None
        elif lk.endswith("beforecount"):
            v[k] = REPLY_BATCH if direction == "before" else None
    return v


class Scraper:
    def __init__(self, templates, out_dir, feedback_id=None, concurrency=20, delay=0.0, retries=6,
                 proxies=None, keep_intent=False, intents=None, log=print, log_progress=True, debug_dir=None):
        self.top = templates[TOP_QUERY]
        reply_tpls = [templates[n] for n in REPLY_QUERIES if n in templates]
        self.reply_tpl = {1: reply_tpls[0] if reply_tpls else None,
                          2: reply_tpls[-1] if reply_tpls else None}
        self.feedback_id = feedback_id
        self.concurrency = concurrency
        self.delay = delay
        self.retries = retries
        self.log = log
        self.log_progress = log_progress
        self.clients = [httpx.AsyncClient(http2=True, proxy=p, timeout=30) for p in (proxies or [None])]

        top_vars = self.top["variables"]
        if "commentsIntentToken" not in top_vars:
            intents = [None]
        elif keep_intent:
            intents = [top_vars["commentsIntentToken"]]
        self.streams = {str(i): {"intent": i, "cursor": None, "done": False, "pages": 0, "new": 0,
                                 "last_size": None, "stop": None}
                        for i in (intents or TOP_INTENTS)}
        self.primary = next(iter(self.streams))
        self.meta = {}

        os.makedirs(out_dir, exist_ok=True)
        self.jsonl_path = os.path.join(out_dir, "comments.jsonl")
        self.state_path = os.path.join(out_dir, "state.json")
        self.seen = set()
        self.reply_done = set()
        self.reply_queued = set()
        self._pending_jobs = {}
        self.queue = asyncio.Queue()
        self.threads = {}               # comment id -> what is needed to fetch its replies
        self.children = Counter()       # comment id -> direct replies written
        self.reply_failed = 0
        self.requests = 0
        self._unsaved_pages = 0
        self.t0 = time.time()
        self._load_state()
        self.out = open(self.jsonl_path, "a", encoding="utf-8")
        self.debug_dir, self._trace_file, self._raw_saved = debug_dir, None, Counter()
        if debug_dir:  # per-page trace + a few raw reply responses, to diagnose what Facebook returned
            os.makedirs(debug_dir, exist_ok=True)
            self._trace_file = open(os.path.join(debug_dir, "trace.jsonl"), "a", encoding="utf-8")

    @property
    def top_done(self):
        return all(s["done"] for s in self.streams.values())

    @property
    def top_pages(self):
        return sum(s["pages"] for s in self.streams.values())

    @property
    def reply_pending(self):
        """Reply threads queued or in flight."""
        return len(self._pending_jobs)

    def stream_summary(self):
        return ", ".join(f"{INTENT_NAMES.get(s['intent'], s['intent'])}: {s['pages']} trang +{s['new']}"
                         f" ({s['stop'] or 'đang chạy'})" for s in self.streams.values())

    # -- persistence ---------------------------------------------------------
    def _load_state(self):
        if os.path.exists(self.jsonl_path):
            for rec in iter_jsonl(self.jsonl_path):
                if "id" in rec:
                    self.seen.add(rec["id"])
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            if "top_cursor" in st:  # written by the single-ordering version
                self.streams[self.primary].update(cursor=st["top_cursor"], done=st.get("top_done", False))
            for key, saved in (st.get("streams") or {}).items():
                if key in self.streams:
                    self.streams[key].update(saved)
            self.meta = st.get("meta") or {}
            self.reply_done = set(st.get("reply_done", []))
            for job in st.get("reply_pending", []):
                self._enqueue(job)
            self.log(f"[resume] {len(self.seen)} comments, {len(st.get('reply_pending', []))} reply jobs pending")

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"streams": self.streams, "meta": self.meta, "reply_done": sorted(self.reply_done),
                       "reply_pending": list(self._pending_jobs.values())}, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def _checkpoint(self):
        self._unsaved_pages += 1
        if self._unsaved_pages >= 5:
            self._unsaved_pages = 0
            self.out.flush()
            self._save_state()

    def _enqueue(self, job):
        if job["id"] in self.reply_done or job["id"] in self.reply_queued:
            return
        self.reply_queued.add(job["id"])
        self._pending_jobs[job["id"]] = job
        self.queue.put_nowait(job)

    # -- request -------------------------------------------------------------
    async def gql(self, tpl, variables):
        form = dict(tpl["form"])
        form["variables"] = json.dumps(variables, separators=(",", ":"))
        delay = 2
        for attempt in range(self.retries):
            client = random.choice(self.clients)
            try:
                r = await client.post(GQL_URL, headers=tpl["headers"], data=form)
                self.requests += 1
                if r.status_code in (429, 500, 502, 503):
                    raise FBError([{"code": r.status_code, "message": f"HTTP {r.status_code}"}])
                r.raise_for_status()
                chunks = parse_chunks(r.text)
                if not chunks:
                    raise FBError([{"code": -1, "message": "empty/non-JSON response: " + r.text[:200]}])
                return chunks
            except (httpx.HTTPError, FBError) as e:
                fatal = isinstance(e, FBError) and e.codes & SESSION_ERROR_CODES
                if fatal or attempt == self.retries - 1:
                    raise
                wait = delay * (2 ** attempt) + random.random()
                self.log(f"  ! {e!s:.160} -> retry in {wait:.1f}s")
                await asyncio.sleep(wait)

    async def _page(self, tpl, variables, parent_hint=None, attempts=3, label=None, save_raw=False):
        """One page. A response with no page_info at all is treated as transient, not as 'no more pages'."""
        for attempt in range(attempts):
            chunks = await self.gql(tpl, variables)
            records, pi, conn = page_from_chunks(chunks, parent_hint)
            self._trace(tpl, variables, label, chunks, records, pi, conn, save_raw)
            if pi is not None or attempt == attempts - 1:
                return records, pi, conn
            self.log(f"  ! response without page_info -> retry {attempt + 1}")
            await asyncio.sleep(1.5 * (attempt + 1))

    def _trace(self, tpl, variables, label, chunks, records, pi, conn, save_raw):
        if not self._trace_file:
            return
        name = tpl["form"].get("fb_api_req_friendly_name", "?")
        node = next((c["data"]["node"] for c in chunks
                     if isinstance(c.get("data"), dict) and isinstance(c["data"].get("node"), dict)), None)
        hide = lambda k, v: bool(v) if any(s in k.lower() for s in ("cursor", "token", "id")) else v
        self._trace_file.write(json.dumps({
            "t": round(time.time() - self.t0, 2), "q": name, "label": label, "records": len(records),
            "depths": dict(Counter(str(r["depth"]) for r in records)),
            "page_info": {k: hide(k, v) for k, v in pi.items()} if pi is not None else None,
            "conn": {k: conn[k] for k in (*CONN_META_KEYS, "behind_the_fold_subtext")
                     if conn and conn.get(k) not in (None, "", False)},
            "node_keys": sorted(node)[:40] if node else None,
            "vars": {k: hide(k, v) for k, v in variables.items() if not k.startswith("__relay")},
        }, ensure_ascii=False) + "\n")
        if save_raw and name != TOP_QUERY and self._raw_saved[name] < 4:
            self._raw_saved[name] += 1
            with open(os.path.join(self.debug_dir, f"{name}_{self._raw_saved[name]}.json"), "w", encoding="utf-8") as f:
                json.dump({"label": label, "variables": variables, "chunks": chunks}, f, ensure_ascii=False)

    def _emit(self, records, parent_hint=None):
        new = 0
        for rec in records:
            if rec["id"] is None:
                continue
            if rec["parent_id"] is None and parent_hint is not None:
                rec["parent_id"] = parent_hint
            if rec["id"] not in self.seen:
                self.seen.add(rec["id"])
                self.out.write(json.dumps({k: v for k, v in rec.items() if not k.startswith("_")},
                                          ensure_ascii=False) + "\n")
                new += 1
                if rec["parent_id"]:
                    self.children[rec["parent_id"]] += 1
            if rec["_feedback_id"] and rec["_expansion_token"]:
                thread = {"id": rec["id"], "feedback_id": rec["_feedback_id"], "token": rec["_expansion_token"],
                          "depth": rec["depth"] or 0, "total": rec["reply_total"] or 0}
                self.threads.setdefault(rec["id"], thread)
                # schedule replies for any comment that has (or may have) them
                known = [c for c in (rec["reply_count"], rec["reply_total"]) if c is not None]
                if not known or max(known) > 0:
                    self._enqueue(thread)
            # Facebook reports 0 for many replies that do have replies of their own: a child proves it
            if rec["parent_id"] in self.threads:
                self._enqueue(self.threads[rec["parent_id"]])
        return new

    # -- top-level comments: one sequential cursor chain per ordering -----------
    async def run_top(self):
        tasks = [asyncio.create_task(self._run_stream(key)) for key, s in self.streams.items() if not s["done"]]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.log(f"[top] done, total so far {len(self.seen)} | {self.stream_summary()}")

    async def _run_stream(self, key):
        s = self.streams[key]
        variables = dict(self.top["variables"])
        if self.feedback_id:
            variables["id"] = self.feedback_id
        if "focusCommentID" in variables:
            variables["focusCommentID"] = None
        if s["intent"] is not None:
            variables["commentsIntentToken"] = s["intent"]
        try:
            while not s["done"]:
                records, pi, conn = await self._page(self.top, set_cursor(variables, s["cursor"]))
                s["new"] += self._emit(records)
                s["pages"] += 1
                s["last_size"] = len(records)
                if conn and key == self.primary and not self.meta:
                    self.meta = {k: conn[k] for k in CONN_META_KEYS if k in conn}
                cursor = (pi or {}).get("end_cursor")
                if pi is None:
                    s.update(done=True, stop="no_page_info")
                elif not pi.get("has_next_page"):
                    s.update(done=True, stop="end")
                elif not cursor or cursor == s["cursor"]:
                    s.update(done=True, stop="cursor_stuck")
                s["cursor"] = cursor or s["cursor"]
                self._checkpoint()
                if self.delay:
                    await asyncio.sleep(self.delay)
        except Exception as e:
            if key == self.primary:
                raise
            # the extra ordering is a bonus: if Facebook rejects it, the job carries on without it
            s.update(done=True, stop=f"error: {e!s:.120}")
            self.log(f"  ! ordering {s['intent']} stopped: {e!s:.160}")

    # -- replies (parallel) -----------------------------------------------------
    async def reply_worker(self):
        while True:
            job = await self.queue.get()
            try:
                await self._fetch_replies(job)
            except Exception as e:  # keep job in pending so a resume retries it
                self.reply_failed += 1
                self.log(f"  ! replies of {job['id']} failed: {e!s:.200}")
            finally:
                self.queue.task_done()

    async def _fetch_replies(self, job):
        depth1 = self.reply_tpl[1]
        tpl = self.reply_tpl[1 if job["depth"] == 0 else 2]
        if tpl is None:
            return
        await self._paginate_thread(tpl, job, "replies" if tpl is depth1 else "sub_replies")
        if tpl is not depth1 and depth1 is not None and self.children[job["id"]] < job.get("total", 0):
            # the Depth2 query stops after its first page on some threads; Depth1 reads the same reply list
            await self._paginate_thread(depth1, job, "sub_replies_via_depth1")
        self.reply_done.add(job["id"])
        self._pending_jobs.pop(job["id"], None)

    async def _paginate_thread(self, tpl, job, label):
        variables = dict(tpl["variables"])
        variables["id"] = job["feedback_id"]
        if "focusCommentID" in variables:
            variables["focusCommentID"] = None
        for k in variables:
            if k.lower().endswith("expansiontoken"):
                variables[k] = job["token"]
        save_raw = job.get("total", 0) >= 5
        seen_cursors, first_pi, cursor = set(), None, None
        while True:  # forward from wherever the thread opens
            records, pi, _ = await self._page(tpl, reply_page_vars(variables, cursor, "after"), job["id"],
                                              attempts=2, label=label, save_raw=save_raw)
            self._emit(records, parent_hint=job["id"])
            if first_pi is None:
                first_pi = pi or {}
            cursor = (pi or {}).get("end_cursor")
            if not pi or not pi.get("has_next_page") or not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            if self.delay:
                await asyncio.sleep(self.delay)
        cursor = first_pi.get("start_cursor") if first_pi.get("has_previous_page") else None
        while cursor and cursor not in seen_cursors:  # then backward, for threads that open mid-way
            seen_cursors.add(cursor)
            records, pi, _ = await self._page(tpl, reply_page_vars(variables, cursor, "before"), job["id"],
                                              attempts=2, label=label + "_back", save_raw=save_raw)
            self._emit(records, parent_hint=job["id"])
            cursor = (pi or {}).get("start_cursor") if (pi or {}).get("has_previous_page") else None
            if self.delay:
                await asyncio.sleep(self.delay)

    # -- orchestration ----------------------------------------------------------
    async def progress(self):
        while True:
            await asyncio.sleep(10)
            if self.log_progress:
                el = time.time() - self.t0
                self.log(f"[{el:6.0f}s] comments={len(self.seen):>6}  req={self.requests:>5}  "
                         f"reply_queue={self.queue.qsize():>5}  rate={len(self.seen) / max(el, 1):.1f}/s")
            self.out.flush()
            self._save_state()

    async def run(self):
        workers = [asyncio.create_task(self.reply_worker()) for _ in range(self.concurrency)]
        prog = asyncio.create_task(self.progress())
        try:
            await self.run_top()
            await self.queue.join()
        finally:
            for t in workers + [prog]:
                t.cancel()
            await asyncio.gather(*workers, prog, return_exceptions=True)
            self.out.flush()
            self._save_state()
            self.out.close()
            if self._trace_file:
                self._trace_file.close()
            for c in self.clients:
                await c.aclose()
        el = time.time() - self.t0
        self.log(f"[done] {len(self.seen)} comments in {el:.0f}s ({self.requests} requests)")


def iter_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:  # e.g. a half-written last line while scraping
                pass


def completeness(out_dir):
    """What was scraped vs what Facebook declared, so missing data can be told apart from hidden data."""
    rows = list(iter_jsonl(os.path.join(out_dir, "comments.jsonl")))
    parent_of = {r.get("id"): r.get("parent_id") for r in rows}
    descendants = Counter()
    for r in rows:
        p, hops = r.get("parent_id"), 0
        while p and hops < 10:  # a thread's reply_total counts replies of replies too
            descendants[p] += 1
            p, hops = parent_of.get(p), hops + 1
    top = [r for r in rows if not r.get("parent_id")]
    declared = sum(r.get("reply_total") or 0 for r in top)
    short = [r for r in top if (r.get("reply_total") or 0) > descendants[r.get("id")]]
    return {
        "comments": len(rows), "top_level": len(top), "replies": len(rows) - len(top),
        "replies_declared": declared,
        "replies_missing": sum((r.get("reply_total") or 0) - descendants[r.get("id")] for r in short),
        "threads_short": len(short),
        "threads_empty": sum(1 for r in short if not descendants[r.get("id")]),
    }


def export_csv(out_dir):
    """Write comments.csv next to comments.jsonl; returns the number of rows."""
    rows = list(iter_jsonl(os.path.join(out_dir, "comments.jsonl")))
    if not rows:
        return 0
    fields = list(dict.fromkeys(k for r in rows for k in r))  # union: rows from older runs may lack new columns
    with open(os.path.join(out_dir, "comments.csv"), "w", newline="", encoding="utf-8-sig") as f:  # Excel-friendly
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows console: in tiếng Việt không lỗi
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Scrape Facebook comments + replies")
    ap.add_argument("--har", help="HAR từ DevTools; bỏ trống thì tự lấy session bằng Chrome ẩn")
    ap.add_argument("--out", default="out", help="output directory (re-run same dir to resume)")
    ap.add_argument("--url", help="link bài viết (hoặc post ID / feedback ID) cần cào")
    ap.add_argument("--post-id", help="scrape a different post (numeric id) with the same HAR session")
    ap.add_argument("--concurrency", type=int, default=20, help="parallel reply workers")
    ap.add_argument("--delay", type=float, default=0.0, help="sleep between pages per worker (s)")
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--proxies", help="file with one proxy URL per line (http://user:pass@host:port)")
    ap.add_argument("--keep-intent", action="store_true", help="only the sort order from the session, no extra orderings")
    ap.add_argument("--inspect", action="store_true", help="only list GraphQL queries found in the HAR")
    args = ap.parse_args()
    if not args.har and not args.url:
        ap.error("cần --url (và tuỳ chọn --har)")

    templates = None
    if args.har:
        templates = load_templates(args.har)
        if args.inspect or TOP_QUERY not in templates:
            print("GraphQL queries in HAR:")
            for name, t in templates.items():
                print(f"  {name:55} doc_id={t['form'].get('doc_id')}  vars={list(t['variables'])}")
            if TOP_QUERY not in templates:
                sys.exit(f"\n'{TOP_QUERY}' not found: bấm 'Xem thêm bình luận' trước khi export HAR.")
            return
        if not any(n in templates for n in REPLY_QUERIES):
            print("! Không thấy query reply trong HAR -> chỉ lấy comment cấp 1 (+ reply preview).")
        if "cookie" not in templates[TOP_QUERY]["headers"]:
            print("! HAR không có cookie: hãy export 'with sensitive data', nếu không FB có thể trả ít dữ liệu.")

    proxies = None
    if args.proxies:
        with open(args.proxies, encoding="utf-8") as f:
            proxies = [l.strip() for l in f if l.strip()] or None

    async def go():
        nonlocal templates
        feedback_id = encode_feedback(args.post_id) if args.post_id else None
        proxy = proxies[0] if proxies else None
        if templates is None:
            import fb_browser  # needs playwright
            pool = fb_browser.BrowserPool()
            try:
                info = await pool.open_post(args.url, proxy=proxy)
            finally:
                await pool.close()
            templates, feedback_id = info["templates"], info["feedback_id"]
            print(f"[post] {info['title']!r}  post_id={info['post_id']}  ~{info['total_hint']} comments ({info['mode']})")
        elif args.url:
            info = await resolve_target(templates, args.url, proxy=proxy)
            apply_tokens(templates, info["fb_dtsg"], info["lsd"], info["final_url"])
            feedback_id = info["feedback_id"]
            print(f"[post] {info['title']!r}  post_id={info['post_id']}  ~{info['total_hint']} comments")
        await Scraper(templates, args.out, feedback_id=feedback_id, concurrency=args.concurrency,
                      delay=args.delay, retries=args.retries, proxies=proxies,
                      keep_intent=args.keep_intent).run()

    asyncio.run(go())
    n = export_csv(args.out)
    if n:
        print(f"[csv] {n} rows -> {os.path.join(args.out, 'comments.csv')}")
        print("[check]", json.dumps(completeness(args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
