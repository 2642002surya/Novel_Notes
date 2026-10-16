"""Best-effort extraction of novel metadata from an HTML page.

Pure standard library (no network, no discord), so it is easy to test.

Novel sites have no common schema, so every field is a heuristic and may come
back empty. Callers must treat ``None`` as "unknown", never as an error.
Sources, in order of trust: JSON-LD, Open Graph / meta tags, then visible
"Author: ..." / "Status: ..." labels and chapter links in the page body.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_ANCHORS = 5000
MAX_TEXT_CHUNKS = 6000

_CHAPTER_RE = re.compile(
    r"(?:chapter|chap\.?|ch\.?|episode|ep\.?)\s*[#:\-]?\s*(\d+(?:\.\d+)?)", re.I
)
_URL_CHAPTER_RE = re.compile(
    r"(?:chapter|chap|ch|episode|ep)[-_/ ]?(\d+(?:[.-]\d+)?)(?:/|$|\.|\?|-)", re.I
)
_STATUS_VALUE_RE = re.compile(
    r"\b(ongoing|on-?going|completed?|finished|hiatus|on hold|dropped|cancell?ed)\b", re.I
)
_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " « ", " » ")


@dataclass
class PageMeta:
    title: str | None = None
    author: str | None = None
    status: str | None = None
    description: str | None = None
    cover_url: str | None = None
    site_name: str | None = None
    icon_url: str | None = None
    latest_chapter: str | None = None
    latest_chapter_url: str | None = None
    released_at: datetime | None = None

    def is_empty(self) -> bool:
        return not any(
            (self.title, self.author, self.cover_url, self.latest_chapter, self.description)
        )


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: dict[str, str] = {}
        self.title = ""
        self.icons: list[tuple[str, str]] = []  # (rel, href)
        self.anchors: list[tuple[str, str]] = []  # (href, text)
        self.ld_json: list[str] = []
        self.chunks: list[str] = []
        self.h1 = ""
        self._in_title = False
        self._in_script_ld = False
        self._skip = 0  # inside <script>/<style>/<noscript>
        self._in_h1 = False
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.metas:
                self.metas[key] = a["content"].strip()
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "icon" in rel and a.get("href"):
                self.icons.append((rel, a["href"]))
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            if "ld+json" in a.get("type", "").lower():
                self._in_script_ld = True
                self.ld_json.append("")
            else:
                self._skip += 1
        elif tag in ("style", "noscript"):
            self._skip += 1
        elif tag == "h1" and not self.h1:
            self._in_h1 = True
        elif tag == "a" and a.get("href") and len(self.anchors) < MAX_ANCHORS:
            self._anchor_href = a["href"]
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            if self._in_script_ld:
                self._in_script_ld = False
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "a" and self._anchor_href is not None:
            text = " ".join("".join(self._anchor_text).split())
            self.anchors.append((self._anchor_href, text))
            self._anchor_href = None

    def handle_data(self, data: str) -> None:
        if self._in_script_ld:
            self.ld_json[-1] += data
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_h1 and not self.h1:
            self.h1 = text
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if len(self.chunks) < MAX_TEXT_CHUNKS:
            self.chunks.append(text)


# --------------------------------------------------------------------- helpers


def _clean(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _http_url(base: str, candidate: str | None) -> str | None:
    if not candidate:
        return None
    try:
        absolute = urljoin(base, candidate.strip())
        parts = urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or len(absolute) > 2000:
        return None
    return absolute


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_status(raw: str | None) -> str | None:
    if not raw:
        return None
    match = _STATUS_VALUE_RE.search(raw)
    if not match:
        return None
    word = match.group(1).lower().replace("-", "")
    if word.startswith(("complet", "finish")):
        return "Completed"
    if word.startswith("ongoing"):
        return "Ongoing"
    if word == "hiatus":
        return "Hiatus"
    if word == "on hold":
        return "On Hold"
    return "Dropped"


def _iter_ld_nodes(blobs: list[str]):
    for blob in blobs:
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if graph:
                    stack.append(graph)


def _ld_name(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _ld_name(value.get("name"))
    if isinstance(value, list):
        for item in value:
            name = _ld_name(item)
            if name:
                return name
    return None


def _ld_image(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _ld_image(value.get("url"))
    if isinstance(value, list):
        for item in value:
            found = _ld_image(item)
            if found:
                return found
    return None


def _strip_site_suffix(title: str, site_name: str | None) -> str:
    for sep in _SEPARATORS:
        if sep in title:
            head, _, tail = title.rpartition(sep)
            tail_l = tail.strip().lower()
            if head and (
                (site_name and tail_l == site_name.lower())
                or ("." in tail_l and " " not in tail_l)  # looks like a domain
            ):
                return head.strip()
    return title


def _labelled_value(chunks: list[str], label: str) -> str | None:
    """Find 'Label: value' in one chunk, or 'Label' followed by a value chunk."""
    inline = re.compile(rf"^{label}s?\s*[:：\-–|]\s*(.+)$", re.I)
    bare = re.compile(rf"^{label}s?\s*[:：]?$", re.I)
    for i, chunk in enumerate(chunks):
        if len(chunk) > 120:
            continue
        match = inline.match(chunk)
        if match:
            return match.group(1).strip()
        if bare.match(chunk):
            for nxt in chunks[i + 1 : i + 3]:
                if nxt and not bare.match(nxt) and len(nxt) <= 80:
                    return nxt.strip()
    return None


def _chapter_number(text: str, href: str) -> float | None:
    match = _CHAPTER_RE.search(text)
    if match:
        return float(match.group(1))
    match = _URL_CHAPTER_RE.search(href)
    if match:
        try:
            return float(match.group(1).replace("-", "."))
        except ValueError:
            return None
    return None


def _latest_chapter(
    anchors: list[tuple[str, str]], base_url: str
) -> tuple[str | None, str | None]:
    best: tuple[float, str, str] | None = None
    for href, text in anchors:
        if len(text) > 160:
            continue
        number = _chapter_number(text, href)
        if number is None:
            continue
        # Only trust anchors that are visibly chapter links.
        if not (_CHAPTER_RE.search(text) or _URL_CHAPTER_RE.search(href)):
            continue
        url = _http_url(base_url, href)
        if url is None:
            continue
        if best is None or number > best[0]:
            label = text if _CHAPTER_RE.search(text) else f"Chapter {number:g}"
            best = (number, label, url)
    if best is None:
        return None, None
    return _clean(best[1], 100), best[2]


# ------------------------------------------------------------------------ main


def parse_page(html: str, page_url: str) -> PageMeta:
    collector = _Collector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # malformed HTML must never break a scan
        pass

    metas = collector.metas
    meta = PageMeta()

    ld_title = ld_author = ld_image = ld_desc = None
    ld_date = None
    for node in _iter_ld_nodes(collector.ld_json):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if not any(
            isinstance(t, str)
            and t in {"Book", "Article", "CreativeWork", "NewsArticle", "BlogPosting",
                      "WebPage", "CreativeWorkSeries", "Periodical", "ComicSeries"}
            for t in types
        ):
            continue
        ld_title = ld_title or _ld_name(node.get("name") or node.get("headline"))
        ld_author = ld_author or _ld_name(node.get("author"))
        ld_image = ld_image or _ld_image(node.get("image"))
        ld_desc = ld_desc or _ld_name(node.get("description"))
        ld_date = ld_date or node.get("dateModified") or node.get("datePublished")

    site_name = _clean(metas.get("og:site_name") or metas.get("application-name"), 80)
    meta.site_name = site_name

    raw_title = metas.get("og:title") or ld_title or collector.h1 or collector.title
    if raw_title:
        raw_title = _strip_site_suffix(" ".join(raw_title.split()), site_name)
    meta.title = _clean(raw_title, 200)

    author = (
        ld_author
        or metas.get("book:author")
        or metas.get("author")
        or metas.get("article:author")
        or metas.get("twitter:creator")
    )
    if author and author.lower().startswith(("http://", "https://")):
        author = None
    if not author:
        author = _labelled_value(collector.chunks, "author") or _labelled_value(
            collector.chunks, "writer"
        )
    meta.author = _clean(author, 80)

    meta.description = _clean(
        metas.get("og:description") or metas.get("description") or ld_desc, 500
    )

    meta.cover_url = _http_url(
        page_url, metas.get("og:image") or metas.get("twitter:image") or ld_image
    )

    status_raw = _labelled_value(collector.chunks, "status")
    meta.status = normalize_status(status_raw) or normalize_status(
        metas.get("og:description") or metas.get("description")
    )

    # Prefer large PNG/apple-touch icons; Discord cannot render .ico reliably.
    icon_href = None
    for rel, href in collector.icons:
        if "apple-touch" in rel:
            icon_href = href
            break
    if icon_href is None:
        for _rel, href in collector.icons:
            if not href.lower().split("?")[0].endswith(".ico"):
                icon_href = href
                break
    if icon_href is None and collector.icons:
        icon_href = collector.icons[0][1]
    meta.icon_url = _http_url(page_url, icon_href)

    chapter, chapter_url = _latest_chapter(collector.anchors, page_url)
    if chapter is None:
        # The page itself may be a chapter page ("... Chapter 12").
        for candidate in (metas.get("og:title"), collector.h1, collector.title):
            if candidate and _CHAPTER_RE.search(candidate):
                found = _CHAPTER_RE.search(candidate)
                chapter = f"Chapter {float(found.group(1)):g}"
                chapter_url = page_url
                break
    meta.latest_chapter = chapter
    meta.latest_chapter_url = chapter_url

    meta.released_at = _parse_dt(
        metas.get("article:modified_time")
        or metas.get("og:updated_time")
        or metas.get("article:published_time")
        or ld_date
    )
    return meta
