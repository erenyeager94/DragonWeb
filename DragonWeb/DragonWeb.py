import os
import re
import json
import time
import hashlib
import posixpath
import random
import threading
import html
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices


# ===================== ASCII HEADER =====================
ASCII_ART = r"""

██████╗ ██████╗  █████╗  ██████╗  ██████╗ ███╗   ██╗██╗    ██╗███████╗██████╗ 
██╔══██╗██╔══██╗██╔══██╗██╔════╝ ██╔═══██╗████╗  ██║██║    ██║██╔════╝██╔══██╗
██║  ██║██████╔╝███████║██║  ███╗██║   ██║██╔██╗ ██║██║ █╗ ██║█████╗  ██████╔╝
██║  ██║██╔══██╗██╔══██║██║   ██║██║   ██║██║╚██╗██║██║███╗██║██╔══╝  ██╔══██╗
██████╔╝██║  ██║██║  ██║╚██████╔╝╚██████╔╝██║ ╚████║╚███╔███╔╝███████╗██████╔╝
╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝ ╚══╝╚══╝ ╚══════╝╚═════╝ 
                                                                              
""".strip("\n")

ASCII_FONT_PX = 15
ASCII_LINE_HEIGHT = 1.06
ASCII_ANIM_MS = 90


# ===================== CONFIG (Mirror Engine) =====================
DOWNLOAD_ROOT = os.path.abspath("DragonWeb_Downloads")  # ✅ absolute now
TIMEOUT = 30

MAX_HTML_PAGES = 2000
MAX_TOTAL_FILES = 20000
MAX_FILE_SIZE_MB = 200

CRAWL_PAGES_SAME_HOST_ONLY = True
ALLOW_EXTERNAL_ASSETS = True
INCLUDE_MEDIA = True
DOWNLOAD_SOURCEMAPS = True
TRY_SITEMAP = True
MAX_SITEMAP_URLS = 15000

SLEEP_BETWEEN_REQUESTS = 0.0

BAD_SCHEMES = ("data:", "mailto:", "javascript:", "tel:", "about:", "blob:")

FONT_EXTS = (".woff2", ".woff", ".ttf", ".otf", ".eot")
IMG_EXTS  = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp", ".avif")
CSS_EXTS  = (".css", ".scss")
JS_EXTS   = (".js", ".mjs")
PAGE_EXTS = (".html", ".htm", ".php", ".asp", ".aspx", ".jsp")
MAP_EXTS  = (".map",)
MEDIA_EXTS = (".mp4", ".webm", ".mp3", ".wav", ".ogg", ".m4a", ".mov", ".m3u8", ".ts")

URL_IN_CSS_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.IGNORECASE)
IMPORT_IN_CSS_RE = re.compile(r"""@import\s+(?:url\()?['"]?(.*?)['"]?\)?\s*;""", re.IGNORECASE)
SOURCEMAP_CSS_RE = re.compile(r"""/\*#\s*sourceMappingURL=(.*?)\s*\*/""", re.IGNORECASE)
SOURCEMAP_JS_RE  = re.compile(r"""//#\s*sourceMappingURL=(.*?)\s*$""", re.IGNORECASE | re.MULTILINE)

JS_ASSET_RE = re.compile(
    r"""(?:
        ["']
        (
            (?:https?:\/\/|\/|\.\/|\.\.\/)
            [^"'<>\\\n\r]+?
            \.(?:css|scss|js|mjs|json|png|jpe?g|webp|gif|svg|ico|woff2?|ttf|otf|eot|map|html?|txt|xml|mp4|webm|mp3|m3u8|ts)
            (?:\?[^"']*)?
        )
        ["']
    )""",
    re.IGNORECASE | re.VERBOSE
)


def build_session() -> requests.Session:
    s = requests.Session()
    # ✅ Use a very common UA (custom UA often gets blocked)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def norm_url(u: str, base: str | None = None) -> str | None:
    if not u:
        return None
    u = u.strip()
    if not u or u.startswith("#") or u.startswith(BAD_SCHEMES):
        return None
    if u.startswith("//"):
        u = "https:" + u
    if base:
        u = urljoin(base, u)
    u, _frag = urldefrag(u)
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return None
    return u


def safe_filename_part(s: str) -> str:
    s = (s or "").strip()
    for bad in ['<', '>', ':', '"', '\\', '|', '?', '*']:
        s = s.replace(bad, "_")
    return s


def add_query_hash(path: str, query: str) -> str:
    if not query:
        return path
    h = hashlib.sha1(query.encode("utf-8", errors="ignore")).hexdigest()[:10]
    root, ext = os.path.splitext(path)
    return f"{root}__q_{h}{ext}"


def guess_ext_by_hint(hint: str | None) -> str | None:
    return {
        "css": ".css",
        "scss": ".scss",
        "js": ".js",
        "font": ".woff2",
        "page": ".html",
        "media": ".mp4",
        "json": ".json",
        "xml": ".xml",
    }.get(hint or "", None)


def make_local_path(download_root: str, url: str, hint: str | None = None) -> str:
    p = urlparse(url)
    host = (p.netloc or "external").replace(":", "_")
    host_dir = os.path.join(download_root, host)

    path = p.path or "/"
    path = posixpath.normpath(path)
    if path.startswith("../"):
        path = "/" + path.lstrip("../")

    if path.endswith("/"):
        path += "index.html"

    _base, ext = os.path.splitext(path)
    if ext == "":
        hinted = guess_ext_by_hint(hint)
        if hinted:
            path = path + hinted
        else:
            # if no ext and not a directory, keep it as file-ish
            path = path + ".bin"

    path = add_query_hash(path, p.query)
    parts = [safe_filename_part(x) for x in path.split("/") if x.strip()]
    return os.path.join(host_dir, *parts)


def rel_link(from_file: str, to_file: str) -> str:
    rel = os.path.relpath(to_file, start=os.path.dirname(from_file))
    return rel.replace(os.sep, "/")


def parse_srcset(value: str) -> list[str]:
    out = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        url_part = item.split()[0].strip()
        if url_part:
            out.append(url_part)
    return out


def extract_from_css(base_url: str, css_text: str) -> set[str]:
    urls = set()
    for m in URL_IN_CSS_RE.finditer(css_text):
        u = (m.group(2) or "").strip()
        nu = norm_url(u, base_url)
        if nu:
            urls.add(nu)

    for m in IMPORT_IN_CSS_RE.finditer(css_text):
        u = (m.group(1) or "").strip()
        nu = norm_url(u, base_url)
        if nu:
            urls.add(nu)

    return urls


def maybe_find_sourcemap(base_url: str, text: str, kind: str) -> str | None:
    if not DOWNLOAD_SOURCEMAPS:
        return None
    if kind == "css":
        m = SOURCEMAP_CSS_RE.search(text)
        if m:
            return norm_url(m.group(1).strip(), base_url)
    if kind == "js":
        m = SOURCEMAP_JS_RE.search(text)
        if m:
            return norm_url(m.group(1).strip(), base_url)
    return None


def rewrite_css_urls(base_url: str, css_text: str, current_local_file: str, resolver) -> str:
    def repl_url(m):
        raw = (m.group(2) or "").strip()
        nu = norm_url(raw, base_url)
        if not nu:
            return m.group(0)
        target_local = resolver(nu, hint=None)
        return f"url('{rel_link(current_local_file, target_local)}')"

    css_text = URL_IN_CSS_RE.sub(repl_url, css_text)

    def repl_import(m):
        raw = (m.group(1) or "").strip()
        nu = norm_url(raw, base_url)
        if not nu:
            return m.group(0)
        target_local = resolver(nu, hint="css")
        return f"@import url('{rel_link(current_local_file, target_local)}');"

    css_text = IMPORT_IN_CSS_RE.sub(repl_import, css_text)
    return css_text


def extract_from_html(base_url: str, html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")

    base_tag = soup.find("base", href=True)
    if base_tag and base_tag.get("href"):
        base_url = urljoin(base_url, base_tag["href"].strip())

    assets = []  # (url, hint)
    pages  = []  # (url, hint)

    def add(u, hint, is_page=False):
        nu = norm_url(u, base_url)
        if not nu:
            return
        if is_page:
            pages.append((nu, hint))
        else:
            assets.append((nu, hint))

    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])).lower()
        href = link["href"]
        if "stylesheet" in rel:
            add(href, "css", is_page=False)
        elif "icon" in rel or "apple-touch-icon" in rel:
            add(href, "img", is_page=False)
        elif "manifest" in rel:
            add(href, "json", is_page=False)
        elif "preload" in rel or "prefetch" in rel or "modulepreload" in rel:
            asv = (link.get("as") or "").lower()
            if asv == "style":
                add(href, "css", False)
            elif asv == "script":
                add(href, "js", False)
            elif asv == "font":
                add(href, "font", False)
            elif asv == "image":
                add(href, "img", False)
            else:
                add(href, None, False)

    for s in soup.find_all("script"):
        if s.get("src"):
            add(s["src"], "js", is_page=False)

    for img in soup.find_all(["img", "image"]):
        for attr in ("src", "data-src", "data-original", "data-lazy", "data-url", "data-bg", "data-background"):
            if img.get(attr):
                add(img.get(attr), "img", is_page=False)
        if img.get("srcset"):
            for u in parse_srcset(img["srcset"]):
                add(u, "img", is_page=False)

    for src in soup.find_all("source"):
        if src.get("src"):
            add(src["src"], "img", is_page=False)
        if src.get("srcset"):
            for u in parse_srcset(src["srcset"]):
                add(u, "img", is_page=False)

    for v in soup.find_all(["video", "audio"]):
        if v.get("src"):
            add(v["src"], "media", is_page=False)
        if v.get("poster"):
            add(v["poster"], "img", is_page=False)

    for fr in soup.find_all("iframe", src=True):
        add(fr["src"], "page", is_page=True)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(BAD_SCHEMES) or href.startswith("#"):
            continue
        add(href, "page", is_page=True)

    for obj in soup.find_all(["object", "embed"]):
        if obj.get("data"):
            add(obj["data"], None, is_page=False)
        if obj.get("src"):
            add(obj["src"], None, is_page=False)

    for meta in soup.find_all("meta", content=True):
        c = (meta.get("content", "") or "").strip()
        if c and (c.startswith("http") or c.startswith("/") or c.startswith("./") or c.startswith("../")):
            if any(ext in c.lower() for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico")):
                add(c, "img", is_page=False)

    for tag in soup.find_all(style=True):
        style = tag.get("style", "")
        for m in URL_IN_CSS_RE.finditer(style):
            add(m.group(2), "img", is_page=False)

    for st in soup.find_all("style"):
        css = st.get_text() or ""
        for u in extract_from_css(base_url, css):
            assets.append((u, None))

    return soup, base_url, assets, pages


def extract_from_js(base_url: str, js_text: str) -> set[str]:
    urls = set()
    for m in JS_ASSET_RE.finditer(js_text):
        raw = (m.group(1) or "").strip()
        nu = norm_url(raw, base_url)
        if nu:
            urls.add(nu)
    return urls


def parse_sitemap_urls(xml_text: str) -> list[str]:
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, flags=re.IGNORECASE)
    out = []
    for loc in locs:
        loc = (loc or "").strip()
        if loc:
            out.append(loc)
    return out


def discover_sitemaps(start_url: str) -> list[str]:
    sitemaps = []
    parsed = urlparse(start_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    sitemaps.append(urljoin(base, "/sitemap.xml"))

    robots = urljoin(base, "/robots.txt")
    try:
        r = build_session().get(robots, timeout=TIMEOUT)
        if r.ok:
            for line in r.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm = line.split(":", 1)[1].strip()
                    if sm:
                        sitemaps.append(sm)
    except Exception:
        pass

    uniq, seen = [], set()
    for s in sitemaps:
        if s not in seen:
            uniq.append(s)
            seen.add(s)
    return uniq


# ===================== ENGINE =====================
@dataclass
class MirrorStats:
    start_time: float
    total_files: int = 0
    html_pages: int = 0
    current_url: str = ""
    target: str = ""
    done: bool = False
    error: str = ""
    failures: int = 0


@dataclass
class QueueItem:
    url: str
    kind: str          # "page" | "asset"
    referer: str | None = None
    hint: str | None = None


class SiteMirror:
    def __init__(self, start_url: str, log_cb, event_cb, stats: MirrorStats, stop_event: threading.Event):
        self.start_url = start_url
        self.root = DOWNLOAD_ROOT
        self.log = log_cb
        self.event = event_cb
        self.stats = stats
        self.stop_event = stop_event

        self.session = build_session()

        self.visited = set()
        self.url_to_local = {}
        self.url_hint = {}

        self.root_netloc = urlparse(start_url).netloc

    def resolve_local(self, url: str, hint: str | None):
        if url not in self.url_hint and hint:
            self.url_hint[url] = hint
        final_hint = self.url_hint.get(url, hint)
        if url not in self.url_to_local:
            self.url_to_local[url] = make_local_path(self.root, url, final_hint)
        return self.url_to_local[url]

    def can_queue_page(self, url: str) -> bool:
        if not CRAWL_PAGES_SAME_HOST_ONLY:
            return True
        return urlparse(url).netloc == self.root_netloc

    def can_queue_asset(self, url: str) -> bool:
        if not ALLOW_EXTERNAL_ASSETS:
            return urlparse(url).netloc == self.root_netloc
        return True

    def should_download_media(self, url: str) -> bool:
        if INCLUDE_MEDIA:
            return True
        low = url.lower().split("?", 1)[0]
        return not low.endswith(MEDIA_EXTS)

    def _classify_ext(self, url: str) -> str:
        p = urlparse(url)
        ext = os.path.splitext((p.path or "").lower())[1]
        return ext

    def fetch_bytes(self, url: str, referer: str | None) -> tuple[requests.Response | None, bytes | None]:
        try:
            headers = {}
            if referer:
                headers["Referer"] = referer
                rp = urlparse(referer)
                headers["Origin"] = f"{rp.scheme}://{rp.netloc}"

            resp = self.session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True, headers=headers)
            status = resp.status_code

            # ✅ log response quick
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            self.log(f"[HTTP] {status} :: {ctype or '-'} :: {resp.url}")

            if status >= 400:
                self.stats.failures += 1
                self.event(f"FAILED {status} :: {url}")
                return None, None

            max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                self.event(f"SKIP (Content-Length too large) :: {url}")
                return None, None

            # ✅ streaming read (works for chunked transfer too)
            data = bytearray()
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if self.stop_event.is_set():
                    return None, None
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > max_bytes:
                    self.event(f"SKIP (Downloaded too large) :: {url}")
                    return None, None

            return resp, bytes(data)

        except Exception as e:
            self.stats.failures += 1
            self.event(f"FAILED :: {url} :: {e}")
            return None, None

    def save_binary(self, local_path: str, content: bytes, url: str | None = None):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content)

        # ✅ show saved path (relative)
        try:
            relp = os.path.relpath(local_path, start=self.root)
        except Exception:
            relp = local_path
        if url:
            self.event(f"SAVED :: {relp}")

    def rewrite_html_inplace(self, soup: BeautifulSoup, base_url: str, current_local: str):
        attr_hints = {
            "script": {"src": "js"},
            "link": {"href": "css"},
            "img": {"src": "img"},
            "source": {"src": "img"},
            "iframe": {"src": "page"},
            "video": {"src": "media", "poster": "img"},
            "audio": {"src": "media"},
            "a": {"href": "page"},
        }

        for tag in soup.find_all(True):
            tname = tag.name.lower()

            if tag.has_attr("srcset"):
                parts = []
                for item in tag["srcset"].split(","):
                    item = item.strip()
                    if not item:
                        continue
                    seg = item.split()
                    u = seg[0]
                    rest = " ".join(seg[1:])
                    nu = norm_url(u, base_url)
                    if nu:
                        target = self.resolve_local(nu, hint="img")
                        u2 = rel_link(current_local, target)
                        parts.append((u2 + (" " + rest if rest else "")).strip())
                    else:
                        parts.append(item)
                tag["srcset"] = ", ".join(parts)

            hints = attr_hints.get(tname, {})
            for attr, hint in hints.items():
                if tag.has_attr(attr):
                    raw = tag.get(attr)
                    nu = norm_url(raw, base_url)
                    if nu:
                        target = self.resolve_local(nu, hint=hint)
                        tag[attr] = rel_link(current_local, target)

            for lazy in ("data-src", "data-original", "data-lazy", "data-url", "data-bg", "data-background"):
                if tag.has_attr(lazy):
                    raw = tag.get(lazy)
                    nu = norm_url(raw, base_url)
                    if nu:
                        target = self.resolve_local(nu, hint="img")
                        tag[lazy] = rel_link(current_local, target)

            if tag.has_attr("style"):
                style = tag.get("style", "")

                def repl(m):
                    raw = (m.group(2) or "").strip()
                    nu = norm_url(raw, base_url)
                    if not nu:
                        return m.group(0)
                    target = self.resolve_local(nu, hint="img")
                    return f"url('{rel_link(current_local, target)}')"

                tag["style"] = URL_IN_CSS_RE.sub(repl, style)

        for st in soup.find_all("style"):
            css = st.get_text() or ""
            css2 = rewrite_css_urls(base_url, css, current_local, self.resolve_local)
            st.string = css2

    def add_sitemap_pages(self, q: deque):
        if not TRY_SITEMAP:
            return
        sitemaps = discover_sitemaps(self.start_url)
        added = 0
        seen = set()

        for sm in sitemaps:
            if self.stop_event.is_set():
                return
            sm = norm_url(sm, None)
            if not sm or sm in seen:
                continue
            seen.add(sm)

            self.log(f"[*] Sitemap probe: {sm}")
            resp, content = self.fetch_bytes(sm, referer=self.start_url)
            if not resp or not content:
                continue

            text = content.decode("utf-8", errors="ignore")
            locs = parse_sitemap_urls(text)

            if locs and ("<sitemapindex" in text.lower()):
                for loc in locs:
                    if added >= MAX_SITEMAP_URLS:
                        break
                    nu = norm_url(loc, None)
                    if nu and self.can_queue_page(nu):
                        q.append(QueueItem(nu, "page", referer=self.start_url, hint="page"))
                        added += 1
                continue

            for loc in locs:
                if added >= MAX_SITEMAP_URLS:
                    break
                nu = norm_url(loc, None)
                if nu and self.can_queue_page(nu):
                    q.append(QueueItem(nu, "page", referer=self.start_url, hint="page"))
                    added += 1

        if added:
            self.event(f"SITEMAP queued {added} pages")

    def run(self):
        q = deque()
        q.append(QueueItem(self.start_url, "page", referer=None, hint="page"))
        self.add_sitemap_pages(q)

        while q and not self.stop_event.is_set():
            if self.stats.total_files >= MAX_TOTAL_FILES:
                self.event(f"STOP :: MAX_TOTAL_FILES reached ({MAX_TOTAL_FILES})")
                break

            item = q.popleft()
            url, kind, referer, hint = item.url, item.kind, item.referer, item.hint

            if url in self.visited:
                continue

            if kind == "page" and not self.can_queue_page(url):
                continue
            if kind == "asset" and not self.can_queue_asset(url):
                continue
            if not self.should_download_media(url):
                continue
            if kind == "page" and self.stats.html_pages >= MAX_HTML_PAGES:
                continue

            self.visited.add(url)
            self.stats.current_url = url

            local_path = self.resolve_local(url, hint=hint)

            self.log(f"[+] GET :: {url}")
            resp, content = self.fetch_bytes(url, referer=referer)
            if not resp:
                continue

            if SLEEP_BETWEEN_REQUESTS > 0:
                time.sleep(SLEEP_BETWEEN_REQUESTS)

            final_url = resp.url
            if final_url != url:
                self.url_to_local[final_url] = local_path
                self.visited.add(final_url)

            ctype = (resp.headers.get("Content-Type") or "").lower()
            low = final_url.lower().split("?", 1)[0]
            ext = self._classify_ext(final_url)

            self.stats.total_files += 1

            # ✅ Strong classification:
            is_css  = ("text/css" in ctype) or ext in CSS_EXTS
            is_js   = ("javascript" in ctype) or ext in JS_EXTS
            is_map  = ext in MAP_EXTS
            is_page_ext = ext in PAGE_EXTS

            # ✅ Only parse HTML when it's truly a page
            is_html_page = (kind == "page") or is_page_ext or ("text/html" in ctype and kind == "page")

            if is_html_page:
                self.stats.html_pages += 1
                html_text = content.decode(resp.encoding or "utf-8", errors="ignore")

                # Cloudflare / anti-bot hint
                low_html = html_text.lower()
                if "cloudflare" in low_html and "challenge" in low_html:
                    self.event("BLOCKED :: Cloudflare challenge detected (needs browser rendering/cookies).")
                    self.save_binary(local_path, content, url=final_url)
                    continue

                soup, base_url, assets, pages = extract_from_html(final_url, html_text)

                for u, h in assets:
                    ulow = u.lower().split("?", 1)[0]
                    if ulow.endswith(".scss"):
                        h = h or "scss"
                    elif ulow.endswith(".css"):
                        h = h or "css"
                    elif ulow.endswith(JS_EXTS):
                        h = h or "js"
                    elif ulow.endswith(FONT_EXTS):
                        h = h or "font"
                    elif ulow.endswith(IMG_EXTS):
                        h = h or "img"
                    elif ulow.endswith(MAP_EXTS):
                        h = h or "other"
                    elif ulow.endswith(MEDIA_EXTS):
                        h = h or "media"

                    self.resolve_local(u, hint=h)
                    q.append(QueueItem(u, "asset", referer=final_url, hint=h))

                for u, _h in pages:
                    if self.can_queue_page(u):
                        q.append(QueueItem(u, "page", referer=final_url, hint="page"))

                self.rewrite_html_inplace(soup, base_url, local_path)
                self.save_binary(local_path, str(soup).encode("utf-8", errors="ignore"), url=final_url)

            elif is_css:
                css_text = content.decode(resp.encoding or "utf-8", errors="ignore")

                for u in extract_from_css(final_url, css_text):
                    self.resolve_local(u, hint=None)
                    q.append(QueueItem(u, "asset", referer=final_url, hint=None))

                sm = maybe_find_sourcemap(final_url, css_text, "css")
                if sm:
                    self.resolve_local(sm, hint="other")
                    q.append(QueueItem(sm, "asset", referer=final_url, hint="other"))

                css_text2 = rewrite_css_urls(final_url, css_text, local_path, self.resolve_local)
                self.save_binary(local_path, css_text2.encode("utf-8", errors="ignore"), url=final_url)

            elif is_js:
                self.save_binary(local_path, content, url=final_url)

                if DOWNLOAD_SOURCEMAPS:
                    txt = content.decode("utf-8", errors="ignore")
                    sm = maybe_find_sourcemap(final_url, txt, "js")
                    if sm:
                        self.resolve_local(sm, hint="other")
                        q.append(QueueItem(sm, "asset", referer=final_url, hint="other"))

                try:
                    txt = content.decode("utf-8", errors="ignore")
                    for u in extract_from_js(final_url, txt):
                        if self.can_queue_asset(u):
                            self.resolve_local(u, hint=None)
                            q.append(QueueItem(u, "asset", referer=final_url, hint=None))
                except Exception:
                    pass

            elif is_map and DOWNLOAD_SOURCEMAPS:
                self.save_binary(local_path, content, url=final_url)

                try:
                    data = json.loads(content.decode("utf-8", errors="ignore"))
                    sources = data.get("sources", [])
                    sourcesContent = data.get("sourcesContent", [])
                    if sources and sourcesContent and len(sources) == len(sourcesContent):
                        base_dir = os.path.dirname(local_path)
                        out_dir = os.path.join(base_dir, "__sourcemap_sources")
                        os.makedirs(out_dir, exist_ok=True)
                        for s, sc in zip(sources, sourcesContent):
                            if not sc:
                                continue
                            name = safe_filename_part(os.path.basename(s) or "source.txt")
                            out_path = os.path.join(out_dir, name)
                            with open(out_path, "w", encoding="utf-8", errors="ignore") as f:
                                f.write(sc)
                except Exception:
                    pass

            else:
                # ✅ asset binary save (images/fonts/media/unknown)
                self.save_binary(local_path, content, url=final_url)

        self.stats.done = True


def output_folder_for(url: str) -> str:
    p = urlparse(url)
    host = (p.netloc or "external").replace(":", "_")
    return os.path.abspath(os.path.join(DOWNLOAD_ROOT, host))


# ===================== UI (Cyber Ops Panel) =====================
class ScanlineOverlay(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._enabled = True
        self._alpha = 26
        self._gap = 5

    def setEnabledOverlay(self, v: bool):
        self._enabled = v
        self.update()

    def paintEvent(self, e):
        if not self._enabled:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        col = QtGui.QColor(255, 0, 0, self._alpha)
        pen = QtGui.QPen(col)
        pen.setWidth(1)
        p.setPen(pen)
        y = 0
        while y < self.height():
            p.drawLine(0, y, self.width(), y)
            y += self._gap


class MirrorWorker(QtCore.QThread):
    sig_log = QtCore.pyqtSignal(str)
    sig_event = QtCore.pyqtSignal(str)
    sig_stats = QtCore.pyqtSignal(dict)
    sig_done = QtCore.pyqtSignal(dict)

    def __init__(self, url: str, stop_event: threading.Event):
        super().__init__()
        self.url = url
        self.stop_event = stop_event
        self.stats = MirrorStats(start_time=time.time(), target=url)

    def log(self, s: str):
        self.sig_log.emit(s)

    def event(self, s: str):
        self.sig_event.emit(s)

    def run(self):
        try:
            os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
            base_netloc = urlparse(self.url).netloc.replace(":", "_")
            os.makedirs(os.path.join(DOWNLOAD_ROOT, base_netloc), exist_ok=True)

            self.log("[BOOT] Operator console online.")
            self.log(f"[BOOT] Output Root: {DOWNLOAD_ROOT}")
            self.log("[BOOT] Initializing mirror engine...")
            self.event("STATUS :: ARMED")

            mirror = SiteMirror(self.url, self.log, self.event, self.stats, self.stop_event)

            def push_loop():
                while not self.stats.done and not self.stop_event.is_set():
                    elapsed = max(0.001, time.time() - self.stats.start_time)
                    speed = self.stats.total_files / elapsed
                    self.sig_stats.emit({
                        "uptime": elapsed,
                        "target": self.url,
                        "files": self.stats.total_files,
                        "html": self.stats.html_pages,
                        "speed": speed,
                        "current": self.stats.current_url,
                        "out": output_folder_for(self.url),
                        "failures": self.stats.failures,
                        "done": False
                    })
                    time.sleep(0.5)

            t = threading.Thread(target=push_loop, daemon=True)
            t.start()

            mirror.run()

            elapsed = max(0.001, time.time() - self.stats.start_time)
            speed = self.stats.total_files / elapsed
            payload = {
                "uptime": elapsed,
                "target": self.url,
                "files": self.stats.total_files,
                "html": self.stats.html_pages,
                "speed": speed,
                "current": self.stats.current_url,
                "out": output_folder_for(self.url),
                "failures": self.stats.failures,
                "done": True,
                "stopped": self.stop_event.is_set(),
            }
            self.sig_done.emit(payload)

        except Exception as e:
            self.sig_done.emit({
                "done": True,
                "error": str(e),
                "out": output_folder_for(self.url)
            })


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("☠️𝔻𝕣𝕒𝕘𝕠𝕟𝕎𝕖𝕓☠️")
        self.setMinimumSize(1400, 600)

        self.worker: MirrorWorker | None = None
        self.stop_event = threading.Event()

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        main = QtWidgets.QHBoxLayout(root)
        main.setContentsMargins(18, 18, 18, 18)
        main.setSpacing(18)

        # -------- Left Panel --------
        left = QtWidgets.QFrame()
        left.setObjectName("panelLeft")
        left.setFixedWidth(330)
        left_l = QtWidgets.QVBoxLayout(left)
        left_l.setSpacing(12)

        top_title = QtWidgets.QLabel("𝔻𝕣𝕒𝕘𝕠𝕟𝕎𝕖𝕓")
        top_title.setObjectName("leftTitle")
        left_l.addWidget(top_title)

        sub = QtWidgets.QLabel("𝙾𝙿𝙴𝚁𝙰𝚃𝙾𝚁 𝙲𝙾𝙽𝚂𝙾𝙻𝙴")
        sub.setObjectName("leftSub")
        left_l.addWidget(sub)

        lbl = QtWidgets.QLabel("𝚃𝙰𝚁𝙶𝙴𝚃 𝚄𝚁𝙻")
        lbl.setObjectName("labelDim")
        left_l.addWidget(lbl)

        self.urlEdit = QtWidgets.QLineEdit("https://example.com")
        self.urlEdit.setObjectName("urlEdit")
        left_l.addWidget(self.urlEdit)

        self.btnStart = QtWidgets.QPushButton("▶ ☠️ 𝕊𝕋𝔸ℝ𝕋 𝔻𝕣𝕒𝕘𝕠𝕟𝕎𝕖𝕓 ☠️")
        self.btnStart.setObjectName("btnStart")
        self.btnStart.clicked.connect(self.on_start)
        left_l.addWidget(self.btnStart)

        self.btnClear = QtWidgets.QPushButton("𝙲𝙻𝙴𝙰𝚁 𝚃𝙴𝚁𝙼𝙸𝙽𝙰𝙻")
        self.btnClear.setObjectName("btnGhost")
        self.btnClear.clicked.connect(self.on_clear)
        left_l.addWidget(self.btnClear)

        self.btnOpen = QtWidgets.QPushButton("𝙾𝙿𝙴𝙽 𝙾𝚄𝚃𝙿𝚄𝚃 𝙵𝙾𝙻𝙳𝙴𝚁")
        self.btnOpen.setObjectName("btnGhost")
        self.btnOpen.setEnabled(False)
        self.btnOpen.clicked.connect(self.on_open_folder)
        left_l.addWidget(self.btnOpen)

        left_l.addSpacing(8)

        self.chkGlitch = QtWidgets.QCheckBox("𝙶𝙻𝙸𝚃𝙲𝙷 𝙼𝙾𝙳𝙴")
        self.chkGlitch.setChecked(True)
        self.chkGlitch.setObjectName("chk")
        self.chkGlitch.toggled.connect(self.update_glitch_state)
        left_l.addWidget(self.chkGlitch)

        self.chkScan = QtWidgets.QCheckBox("𝚂𝙲𝙰𝙽𝙻𝙸𝙽𝙴𝚂 ")
        self.chkScan.setChecked(True)
        self.chkScan.setObjectName("chk")
        self.chkScan.toggled.connect(self.update_scanlines_state)
        left_l.addWidget(self.chkScan)

        left_l.addSpacing(10)

        warn = QtWidgets.QLabel(
            "OPERATION POLICY:\n"
            "• use only on websites you own or have explicit permission to mirror.\n"
            "• large sites can use a lot of disk space.\n"
            "• respect legal / compliance requirements."
        )
        warn.setWordWrap(True)
        warn.setObjectName("warnText")
        left_l.addWidget(warn)

        left_l.addStretch(1)

        self.leftStatus = QtWidgets.QLabel("READY.")
        self.leftStatus.setObjectName("readyText")
        left_l.addWidget(self.leftStatus)

        # -------- Center Panel --------
        centerWrap = QtWidgets.QFrame()
        centerWrap.setObjectName("panelCenter")
        center = QtWidgets.QVBoxLayout(centerWrap)
        center.setSpacing(10)

        self.bigTitle = QtWidgets.QLabel()
        self.bigTitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bigTitle.setObjectName("bigTitle")
        self.bigTitle.setTextFormat(Qt.TextFormat.RichText)
        self.ascii_lines = ASCII_ART.splitlines()
        self._ascii_hl = 0
        self.bigTitle.setText(self.ascii_html(self._ascii_hl))
        center.addWidget(self.bigTitle)

        self.tip = QtWidgets.QLabel("Console armed. Enter URL → START.")
        self.tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tip.setObjectName("tipText")
        center.addWidget(self.tip)

        self.console = QtWidgets.QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setObjectName("console")
        center.addWidget(self.console, 1)

        self.scanOverlay = ScanlineOverlay(centerWrap)
        self.scanOverlay.raise_()

        # -------- Right Panel --------
        right = QtWidgets.QFrame()
        right.setObjectName("panelRight")
        right.setFixedWidth(310)
        right_l = QtWidgets.QVBoxLayout(right)
        right_l.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        ops = QtWidgets.QLabel("𝕆ℙ𝕊 ℙ𝕒𝕟𝕖𝕝")
        ops.setObjectName("opsTitle")
        header.addWidget(ops)
        header.addStretch(1)
        self.statusLabel = QtWidgets.QLabel("STATUS: ARMED")
        self.statusLabel.setObjectName("statusArmed")
        header.addWidget(self.statusLabel)
        right_l.addLayout(header)

        self.lblUptime = self._kv("Uptime", "00:00:00")
        self.lblTarget = self._kv("Target", "-")
        self.lblFiles  = self._kv("Files", "0")
        self.lblHtml   = self._kv("HTML Pages", "0")
        self.lblSpeed  = self._kv("Speed", "0.00 files/sec")
        self.lblFail   = self._kv("Failures", "0")

        right_l.addWidget(self.lblUptime)
        right_l.addWidget(self.lblTarget)
        right_l.addWidget(self.lblFiles)
        right_l.addWidget(self.lblHtml)
        right_l.addWidget(self.lblSpeed)
        right_l.addWidget(self.lblFail)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setObjectName("hr")
        right_l.addWidget(line)

        rec = QtWidgets.QLabel("RECENT EVENTS")
        rec.setObjectName("labelDim")
        right_l.addWidget(rec)

        self.events = QtWidgets.QListWidget()
        self.events.setObjectName("events")
        right_l.addWidget(self.events, 1)

        main.addWidget(left)
        main.addWidget(centerWrap, 1)
        main.addWidget(right)

        self.glitchTimer = QtCore.QTimer(self)
        self.glitchTimer.timeout.connect(self.glitch_tick)
        self.glitchTimer.start(120)

        self.asciiTimer = QtCore.QTimer(self)
        self.asciiTimer.timeout.connect(self.ascii_tick)
        self.asciiTimer.start(ASCII_ANIM_MS)

        self.resizeTimer = QtCore.QTimer(self)
        self.resizeTimer.setSingleShot(True)
        self.resizeTimer.timeout.connect(self.sync_overlay_geometry)

        self.apply_qss()
        self.console_boot()

    def ascii_html(self, highlight_idx: int) -> str:
        lines = self.ascii_lines
        out = []
        for i, ln in enumerate(lines):
            ln_esc = html.escape(ln).replace(" ", "&nbsp;")
            if i == highlight_idx:
                out.append(f"<span style='color: rgba(255,60,60,1); text-shadow: 0 0 10px rgba(255,0,0,0.55);'>{ln_esc}</span>")
            else:
                out.append(f"<span style='color: rgba(255,25,25,0.92);'>{ln_esc}</span>")

        pre = (
            "<div style='text-align:center;'>"
            f"<pre style='margin:0; padding:0; font-family:Consolas, \"Courier New\", monospace;"
            f" font-size:{ASCII_FONT_PX}px; line-height:{ASCII_LINE_HEIGHT}em;'>"
            + "\n".join(out) +
            "</pre></div>"
        )
        return pre

    def ascii_tick(self):
        self._ascii_hl = (self._ascii_hl + 1) % max(1, len(self.ascii_lines))
        self.bigTitle.setText(self.ascii_html(self._ascii_hl))

    def _kv(self, k: str, v: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(f"{k}:  {v}")
        lbl.setObjectName("kv")
        return lbl

    def apply_qss(self):
        self.setStyleSheet("""
        QWidget {
            background: #070708;
            color: #e5e5e5;
            font-family: Consolas, 'Courier New', monospace;
            font-size: 13px;
        }
        #panelLeft, #panelRight, #panelCenter {
            background: rgba(12, 8, 9, 0.92);
            border: 1px solid rgba(255, 0, 0, 0.22);
            border-radius: 14px;
        }
        #panelCenter { border: 1px solid rgba(255, 0, 0, 0.16); }
        #leftTitle { font-size: 22px; font-weight: 800; color: #ff2b2b; }
        #leftSub { color: rgba(255, 60, 60, 0.85); letter-spacing: 1px; }
        #labelDim { color: rgba(255, 80, 80, 0.65); font-weight: 700; }
        #urlEdit {
            padding: 10px 12px; border-radius: 10px;
            border: 1px solid rgba(255, 0, 0, 0.28);
            background: rgba(0, 0, 0, 0.55); color: #ffffff;
        }
        #btnStart {
            padding: 12px 12px; border-radius: 10px;
            border: 1px solid rgba(255,0,0,0.55);
            background: rgba(255, 0, 0, 0.38);
            font-weight: 900; letter-spacing: 1px;
        }
        #btnStart:hover { background: rgba(255, 0, 0, 0.52); }
        #btnStart:pressed { background: rgba(255, 0, 0, 0.30); }

        #btnGhost {
            padding: 10px 12px; border-radius: 10px;
            border: 1px solid rgba(255,0,0,0.24);
            background: rgba(240, 240, 240, 0.10);
        }
        #btnGhost:hover { background: rgba(240, 240, 240, 0.16); }
        #btnGhost:disabled { color: rgba(255,255,255,0.25); }

        #chk { color: rgba(255,255,255,0.85); }
        QCheckBox::indicator { width: 16px; height: 16px; }
        QCheckBox::indicator:unchecked {
            border: 1px solid rgba(255,0,0,0.40);
            background: rgba(0,0,0,0.30);
            border-radius: 3px;
        }
        QCheckBox::indicator:checked {
            border: 1px solid rgba(255,0,0,0.75);
            background: rgba(255,0,0,0.55);
            border-radius: 3px;
        }

        #warnText { color: rgba(255, 110, 110, 0.70); line-height: 1.3em; }
        #readyText { color: rgba(255, 0, 0, 0.85); font-weight: 900; letter-spacing: 1px; text-align: center; }
        #bigTitle { padding-top: 6px; padding-bottom: 2px; }
        #tipText { color: rgba(255, 90, 90, 0.75); padding-bottom: 4px; }
        #console {
            background: rgba(0, 0, 0, 0.55);
            border: 1px solid rgba(255,0,0,0.22);
            border-radius: 12px;
            padding: 10px;
            color: #e9e9e9;
        }

        #opsTitle { font-size: 22px; font-weight: 900; color: #ff2b2b; }
        #statusArmed { color: rgba(255, 90, 90, 0.85); font-weight: 900; }
        #kv { color: rgba(255,255,255,0.85); padding: 2px 0px; }
        #hr { border: 0px; background: rgba(255,0,0,0.18); height: 1px; }

        #events {
            background: rgba(0,0,0,0.45);
            border: 1px solid rgba(255,0,0,0.18);
            border-radius: 12px;
            padding: 6px;
        }
        #events::item {
            padding: 6px;
            border-bottom: 1px solid rgba(255,0,0,0.10);
            color: rgba(255,255,255,0.80);
        }
        """)

    def console_boot(self):
        self.console.clear()
        self.append_console("[BOOT] Red Mirror Monster is online...")
        self.append_console("[BOOT] Injecting scanlines + glitch layer...")
        self.append_console("[BOOT] Console armed ✓")

    def append_console(self, s: str):
        self.console.appendPlainText(s)
        bar = self.console.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_event(self, s: str):
        ts = time.strftime("%H:%M:%S")
        item = f"[{ts}] {s}"
        self.events.insertItem(0, item)
        if self.events.count() > 80:
            self.events.takeItem(self.events.count() - 1)

    def update_scanlines_state(self, v: bool):
        self.scanOverlay.setEnabledOverlay(v)

    def update_glitch_state(self, v: bool):
        pass

    def glitch_tick(self):
        if not self.chkGlitch.isChecked():
            return
        if random.random() < 0.03 and self.worker is not None:
            self.append_console(f"[OPS] Link trace ping :: {random.randint(100,999)}")
        if random.random() < 0.08:
            self.tip.setStyleSheet("color: rgba(255,120,120,0.88);")
        else:
            self.tip.setStyleSheet("color: rgba(255,90,90,0.75);")

    def sync_overlay_geometry(self):
        center = self.scanOverlay.parentWidget()
        self.scanOverlay.setGeometry(center.rect())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.resizeTimer.start(40)

    def set_running_ui(self, running: bool):
        self.btnStart.setEnabled(not running)
        self.urlEdit.setEnabled(not running)
        self.btnOpen.setEnabled(not running and os.path.isdir(self.btnOpen.property("out_dir") or ""))
        self.leftStatus.setText("RUNNING..." if running else "READY.")
        self.statusLabel.setText("STATUS: RUNNING" if running else "STATUS: ARMED")

    def on_clear(self):
        self.console.clear()
        self.console_boot()

    def on_open_folder(self):
        out_dir = self.btnOpen.property("out_dir") or ""
        if out_dir and os.path.isdir(out_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))
        else:
            self.add_event("Output folder not available yet.")

    def on_start(self):
        raw = self.urlEdit.text().strip()
        if not raw:
            self.add_event("No URL provided.")
            return
        if not urlparse(raw).scheme:
            raw = "https://" + raw

        self.stop_event.clear()
        self.btnOpen.setProperty("out_dir", "")
        self.btnOpen.setEnabled(False)

        self.set_running_ui(True)
        self.console.clear()
        self.append_console("[BOOT] Operator command received.")
        self.append_console(f"[TARGET] {raw}")
        self.append_console(f"[OUTROOT] {DOWNLOAD_ROOT}")
        self.append_console("[ACTION] Starting mirror process...")

        self.worker = MirrorWorker(raw, self.stop_event)
        self.worker.sig_log.connect(self.append_console)
        self.worker.sig_event.connect(self.add_event)
        self.worker.sig_stats.connect(self.on_stats)
        self.worker.sig_done.connect(self.on_done)
        self.worker.start()

    def on_stats(self, d: dict):
        up = float(d.get("uptime", 0.0))
        self.lblUptime.setText(f"Uptime:  {self.fmt_time(up)}")
        self.lblTarget.setText(f"Target:  {d.get('target','-')}")
        self.lblFiles.setText(f"Files:  {int(d.get('files',0))}")
        self.lblHtml.setText(f"HTML Pages:  {int(d.get('html',0))}")
        self.lblSpeed.setText(f"Speed:  {float(d.get('speed',0.0)):.2f} files/sec")
        self.lblFail.setText(f"Failures:  {int(d.get('failures',0))}")

        out_dir = d.get("out", "")
        if out_dir:
            self.btnOpen.setProperty("out_dir", out_dir)

    def on_done(self, d: dict):
        err = d.get("error", "")
        stopped = d.get("stopped", False)

        if err:
            self.add_event(f"ERROR :: {err}")
            self.append_console(f"[ERROR] {err}")
        else:
            if stopped:
                self.add_event("STOPPED by operator.")
                self.append_console("[DONE] Mirror stopped.")
            else:
                self.add_event("COMPLETE :: Mirror finished successfully.")
                self.append_console("[DONE] Mirror finished successfully.")

            out_dir = d.get("out", "")
            if out_dir:
                self.append_console(f"[OUT] {out_dir}")
            self.append_console(f"[FAILURES] {int(d.get('failures',0))}")

        out_dir = d.get("out", "")
        if out_dir and os.path.isdir(out_dir):
            self.btnOpen.setEnabled(True)

        self.set_running_ui(False)
        self.worker = None

    @staticmethod
    def fmt_time(sec: float) -> str:
        sec = int(sec)
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    app = QtWidgets.QApplication([])
    win = MainWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
