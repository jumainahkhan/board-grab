"""Pinterest board -> ZIP downloader.

Flow:
  POST /api/scan      {"url": "<board link>"}  -> list of pins + a short-lived token
  GET  /api/zip/<tok> -> streams a ZIP of every image, built on the fly

Nothing is written to disk. Images are fetched from Pinterest's CDN and
piped straight into the ZIP stream the browser is downloading.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- settings
MAX_PINS = int(os.getenv("MAX_PINS", "1000"))           # cap per board
SCAN_TIMEOUT = int(os.getenv("SCAN_TIMEOUT", "120"))     # seconds
SCANS_PER_WINDOW = int(os.getenv("SCANS_PER_WINDOW", "10"))
ZIPS_PER_WINDOW = int(os.getenv("ZIPS_PER_WINDOW", "5"))
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "600"))       # seconds
MAX_CONCURRENT_SCANS = int(os.getenv("MAX_CONCURRENT_SCANS", "3"))
TOKEN_TTL = 30 * 60
TRUST_PROXY = os.getenv("TRUST_PROXY", "1") == "1"       # read X-Forwarded-For

PINTEREST_HOST = re.compile(r"(^|\.)pinterest\.[a-z.]{2,8}$")
SHORT_HOSTS = {"pin.it"}
MEDIA_HOST = re.compile(r"^(i|v\d*|s)\.pinimg\.com$")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

BASE = Path(__file__).parent
app = FastAPI(docs_url=None, redoc_url=None)
http = requests.Session()
http.headers["User-Agent"] = UA
scan_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)

# token -> {"expires", "name", "items"}
scans: dict[str, dict] = {}
scans_lock = threading.Lock()


# ---------------------------------------------------------------- rate limit
class RateLimiter:
    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                wait = int(q[0] + self.window - now) + 1
                raise HTTPException(429, f"Too many requests. Try again in {wait // 60 + 1} min.")
            q.append(now)


scan_limit = RateLimiter(SCANS_PER_WINDOW, RATE_WINDOW)
zip_limit = RateLimiter(ZIPS_PER_WINDOW, RATE_WINDOW)


def client_ip(req: Request) -> str:
    if TRUST_PROXY and (fwd := req.headers.get("x-forwarded-for")):
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


# ---------------------------------------------------------------- helpers
def resolve_board_url(raw: str) -> str:
    """Turn whatever the user pasted into a clean pinterest.com board URL."""
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    url = urlparse(raw)
    host = (url.hostname or "").lower()

    if host in SHORT_HOSTS:
        # Follow redirects manually so we never leave Pinterest's domains.
        current = raw
        for _ in range(5):
            r = http.get(current, allow_redirects=False, timeout=10)
            loc = r.headers.get("location")
            if not loc:
                break
            current = requests.compat.urljoin(current, loc)
            h = (urlparse(current).hostname or "").lower()
            if not (PINTEREST_HOST.search(h) or h in SHORT_HOSTS):
                raise HTTPException(400, "That short link doesn't point to Pinterest.")
            if PINTEREST_HOST.search(h) and not h.startswith("api."):
                break
        url = urlparse(current)
        host = (url.hostname or "").lower()

    if not PINTEREST_HOST.search(host):
        raise HTTPException(400, "Please paste a Pinterest board link (pinterest.com/... or pin.it/...).")

    parts = [p for p in url.path.split("/") if p]
    if not parts or parts[0] in {"pin", "search", "ideas", "today", "_"}:
        raise HTTPException(400, "That looks like a single pin or a page, not a board. "
                                 "Open the board and copy its link.")
    if len(parts) < 2:
        raise HTTPException(400, "That's a profile link. Open one of the boards and copy its link.")
    # user/board[/section]
    return "https://www.pinterest.com/" + "/".join(parts[:3]) + "/"


def run_gallery_dl(board_url: str) -> list:
    cmd = [sys.executable, "-m", "gallery_dl", "--quiet", "-j",
           "--range", f"1-{MAX_PINS}", board_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Pinterest took too long to respond. Try again in a bit.")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        raise HTTPException(502, "Couldn't read that board from Pinterest.")


def safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\s.-]", "", text or "", flags=re.UNICODE).strip()
    text = re.sub(r"\s+", "-", text)
    return text[:limit].strip("-.")


def thumb_of(url: str) -> str:
    return re.sub(r"/originals/", "/236x/", url, count=1)


def purge_expired() -> None:
    now = time.time()
    with scans_lock:
        for t in [t for t, s in scans.items() if s["expires"] < now]:
            del scans[t]


# ---------------------------------------------------------------- routes
class ScanIn(BaseModel):
    url: str


@app.post("/api/scan")
def scan(body: ScanIn, request: Request):
    scan_limit.check(client_ip(request))
    board_url = resolve_board_url(body.url)

    if not scan_slots.acquire(timeout=30):
        raise HTTPException(503, "The server is busy right now. Try again in a minute.")
    try:
        data = run_gallery_dl(board_url)
    finally:
        scan_slots.release()

    items, seen, skipped, board_name = [], set(), 0, ""
    for entry in data:
        if not entry or entry[0] != 3:          # 3 = downloadable file
            continue
        _, url, meta = entry
        host = (urlparse(url).hostname or "").lower()
        if not url.startswith("https://") or not MEDIA_HOST.match(host):
            skipped += 1                          # e.g. streamed videos
            continue
        if url in seen:
            continue
        seen.add(url)
        if not board_name and isinstance(meta.get("board"), dict):
            board_name = meta["board"].get("name", "")
        items.append({
            "url": url,
            "thumb": thumb_of(url),
            "ext": (meta.get("extension") or url.rsplit(".", 1)[-1])[:5].lower(),
            "title": (meta.get("title") or meta.get("grid_title") or "").strip()[:120],
        })

    if not items:
        raise HTTPException(404, "No pins found. Make sure the board is public and the link is right.")

    board_name = board_name or urlparse(board_url).path.strip("/").split("/")[-1]
    token = secrets.token_urlsafe(16)
    purge_expired()
    with scans_lock:
        scans[token] = {"expires": time.time() + TOKEN_TTL, "name": board_name, "items": items}

    return {
        "token": token,
        "board": board_name,
        "count": len(items),
        "skipped": skipped,
        "capped": len(items) >= MAX_PINS,
        "items": [{"thumb": i["thumb"], "full": i["url"], "title": i["title"], "ext": i["ext"]}
                  for i in items],
    }


class _Sink:
    """Write-only buffer the ZipFile streams into. No tell()/seek(), so
    zipfile switches to streaming mode (data descriptors)."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def drain(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def fetch(url: str) -> bytes | None:
    try:
        r = http.get(url, timeout=30)
        if r.status_code == 200:
            return r.content
    except requests.RequestException:
        pass
    return None


def zip_stream(items: list):
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
    width = len(str(len(items)))
    failed = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        # Prefetch a small window ahead so the download stays fast but
        # memory stays bounded.
        window = 8
        futures = deque(pool.submit(fetch, it["url"]) for it in items[:window])
        nxt = window
        for idx, it in enumerate(items, 1):
            data = futures.popleft().result()
            if nxt < len(items):
                futures.append(pool.submit(fetch, items[nxt]["url"]))
                nxt += 1
            if data is None:
                failed.append(it["url"])
                continue
            label = safe_name(it["title"])
            name = f"{idx:0{width}d}{'_' + label if label else ''}.{it['ext']}"
            info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_STORED
            with zf.open(info, "w", force_zip64=len(data) > 2**31) as f:
                f.write(data)
            yield sink.drain()
    if failed:
        zf.writestr("_could_not_download.txt", "\n".join(failed))
    zf.close()
    yield sink.drain()


@app.get("/api/zip/{token}")
def download_zip(token: str, request: Request):
    with scans_lock:
        s = scans.get(token)
    if not s or s["expires"] < time.time():
        raise HTTPException(410, "This link expired. Scan the board again.")
    zip_limit.check(client_ip(request))
    filename = (safe_name(s["name"]) or "pinterest-board") + ".zip"
    return StreamingResponse(
        zip_stream(s["items"]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store"},
    )


@app.exception_handler(HTTPException)
def http_error(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
