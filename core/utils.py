import re
from urllib.parse import urlparse

# ==========================================
# 1. CORE REGEX & DOMAINS
# ==========================================
URL_REGEX = re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+', re.IGNORECASE)

TARGET_DOMAINS = {"novelbin", "novelarrow", "novelping"}

URL_ROUTE_IDENTIFIERS = {
    "novel", "book", "series", "chapter", "read", "c", "manga", 
    "vip", "fiction", "stories", "index.php", "archives", 
    "novel-book", "lightnovel", "webnovel", "novel-list"
}

IGNORED_DOMAINS = {
    "cdn.discordapp.com", "media.discordapp.net", "discord.com",
    "tenor.com", "giphy.com", "imgur.com", "youtube.com",
    "youtu.be", "tiktok.com", "twitter.com", "x.com"
}

IGNORED_EXTENSIONS = (
    ".webp", ".png", ".jpg", ".jpeg", ".gif", 
    ".svg", ".mp4", ".webm", ".mov", ".mp3", ".wav"
)

STOPWORDS = {
    "novel", "book", "arts", "the", "a", "an", "of", "and", "in", 
    "to", "reborn", "chapter", "raw", "scan", "lightnovel", "webnovel",
    "translation", "tl", "official", "eng", "ch"
}

PROTECTED_CATEGORIES = {"completed", "long novels"}

# ==========================================
# 2. LEGACY COMPATIBILITY MAPS (Keeps novel_scanner working)
# ==========================================
COMPLETED_CATEGORY_MAP = {
    "r-18": "R-18 / Smut",
    "harem": "Harem",
    "rich-theme": "Rich Protagonist",
    "fast cultivation system": "Fast Cultivation",
    "romance": "Romance",
    "normal": "Normal / Other"
}

ACTIVE_CATEGORY_MAP = {
    "R-18 / Smut": "🔞・Forbidden Scrolls",
    "Harem": "👑・Harem Emperor",
    "Rich Protagonist": "💰・Golden Fortune",
    "Fast Cultivation": "⚡・Rapid Ascension",
    "Romance": "💖・Romance Path",
    "System / Cheat": "🌌・System Awakening",
    "Normal / Other": "📜・Standard Collection",
    "Plan to Read": "⏳・The Unexplored Realm"
}

ARCHIVE_CATEGORY_NAME = "📚・The Eternal Archives"
ARCHIVE_CHANNEL_MAP = {
    "R-18 / Smut": "🔞・r18-scrolls",
    "Harem": "👑・harem-legends",
    "Rich Protagonist": "💰・wealthy-protagonists",
    "Fast Cultivation": "⚡・rapid-cultivation",
    "Romance": "💖・romance-legends",
    "Normal / Other": "📜・standard-collection"
}

# ==========================================
# 3. CELESTIAL WHITELIST & SUGGESTION MAPS
# ==========================================
CELESTIAL_COMPLETED_MAP = {
    "r-18": ("Already Read", "R-18 / Smut", "🔞・crimson-legends"),
    "harem": ("Already Read", "Harem", "👑・harem-legends"),
    "rich theme": ("Already Read", "Rich Protagonist", "💰・golden-legends"),
    "fast cultivation system": ("Already Read", "Fast Cultivation", "⚡・rapid-ascension"),
    "romance": ("Already Read", "Romance", "💖・eternal-bonds"),
    "normal": ("Already Read", "Normal / Other", "📜・timeless-classics")
}

GENERAL_CHANNELS_MAP = {
    "normal-novels": "Normal / Other",
    "rich-novels": "Rich Protagonist",
    "smut-novels": "R-18 / Smut"
}

def is_source_whitelisted(category_name: str | None, channel_name: str) -> bool:
    """Enforces the 10-source scope whitelist."""
    cat = (category_name or "").strip().lower()
    ch = (channel_name or "").strip().lower()

    if cat == "general":
        return ch in GENERAL_CHANNELS_MAP
    if cat in ("daily updates", "lst", "current reading", "rich theme", "in queue", "to read", "long novels"):
        return True
    if ch in ("daily updates", "lst", "current reading", "rich theme", "in queue", "to read", "long novels"):
        return True
    if "harem theme novels" in cat or "harem theme novels" in ch:
        return True
    if cat == "completed":
        return any(k in ch for k in CELESTIAL_COMPLETED_MAP.keys())

    return False

def determine_source_classification(category_name: str | None, channel_name: str) -> dict:
    cat = (category_name or "").strip().lower()
    ch = (channel_name or "").strip().lower()

    res = {
        "reading_status": "Unassigned",
        "theme": "Normal / Other",
        "destination_category": "📌・The Archive Gates",
        "is_completed": 0,
        "is_favorite": 0
    }

    if cat == "completed":
        res["is_completed"] = 1
        res["reading_status"] = "Already Read"
        res["destination_category"] = "🏆・The Hall of Ascended Legends"
        for k, (status, theme, _) in CELESTIAL_COMPLETED_MAP.items():
            if k in ch:
                res["theme"] = theme
                break
        return res

    if cat == "long novels" or ch == "long novels":
        res["is_favorite"] = 1
        res["destination_category"] = "🌌・The Immortal Treasury"
        return res

    if cat == "daily updates" or ch == "daily updates":
        res["reading_status"] = "Currently Reading"
        res["destination_category"] = "⚡・Daily Chapter Realm"
        return res

    if cat == "lst" or ch == "lst":
        res["reading_status"] = "Currently Reading"
        res["theme"] = "R-18 / Smut"
        res["destination_category"] = "🔞・Crimson Chapter Realm"
        return res

    if cat == "current reading" or ch == "current reading":
        res["reading_status"] = "Moodbound"
        res["destination_category"] = "🌙・The Moodbound Realm"
        return res

    if "harem theme novels" in cat or "harem theme novels" in ch:
        res["reading_status"] = "Moodbound"
        res["theme"] = "Harem"
        res["destination_category"] = "👑・The Harem Imperium"
        return res

    if cat == "rich theme" or ch == "rich theme":
        res["reading_status"] = "Moodbound"
        res["theme"] = "Rich Protagonist"
        res["destination_category"] = "💰・The Golden Dynasty"
        return res

    if cat in ("in queue", "to read") or ch in ("in queue", "to read"):
        res["reading_status"] = "Plan to Read"
        res["destination_category"] = "⏳・The Heavenly Queue"
        return res

    if cat == "general" and ch in GENERAL_CHANNELS_MAP:
        res["reading_status"] = "Unassigned"
        res["theme"] = GENERAL_CHANNELS_MAP[ch]
        res["destination_category"] = "📌・The Archive Gates"
        return res

    return res

# ==========================================
# 4. SHARED SCANNER & PARSING FUNCTIONS
# ==========================================
def is_protected_channel(channel) -> bool:
    if not channel or not getattr(channel, "category", None):
        return False
    cat_name = (channel.category.name or "").strip().lower()
    return cat_name in PROTECTED_CATEGORIES

def is_source_completed_category(category_name: str | None) -> bool:
    if not category_name:
        return False
    return "completed" in category_name.strip().lower()

def map_completed_channel(channel_name: str) -> str:
    clean = channel_name.strip().lower()
    for key, mapped_category in COMPLETED_CATEGORY_MAP.items():
        if key in clean:
            return mapped_category
    return "Normal / Other"

def is_chapter_segment(segment: str) -> bool:
    clean = segment.lower().strip()
    return bool(re.match(r"^(chapter|ch|c|volume|vol|v)[\-_/]?\d+", clean))

def extract_chapter_number(url: str) -> float:
    clean_path = urlparse(url).path.lower()
    match = re.search(r'(?:chapter|ch|c)[-_/]?(\d+(?:\.\d+)?)', clean_path)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    fallback = re.findall(r'\d+', clean_path)
    if fallback:
        try:
            return float(fallback[-1])
        except ValueError:
            pass
    return -1.0

def is_valid_novel_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if any(ignored in hostname for ignored in IGNORED_DOMAINS):
            return False
        clean_path = parsed.path.lower().split("?")[0]
        if clean_path.endswith(IGNORED_EXTENSIONS):
            return False
        segments = [s for s in clean_path.strip("/").split("/") if s]
        return len(segments) >= 1
    except Exception:
        return False

def extract_novel_slug(url: str) -> str:
    if not is_valid_novel_url(url):
        return ""
    path = urlparse(url).path.strip("/")
    parts = [re.sub(r"\.(html|php|htm|aspx)$", "", p.lower()) for p in path.split("/") if p]
    if not parts:
        return ""

    if is_chapter_segment(parts[-1]):
        for candidate in reversed(parts[:-1]):
            if candidate not in URL_ROUTE_IDENTIFIERS and not is_chapter_segment(candidate):
                return re.sub(r"[-_]+", "-", candidate).strip("-")
    else:
        for candidate in reversed(parts):
            if candidate not in URL_ROUTE_IDENTIFIERS and not is_chapter_segment(candidate):
                return re.sub(r"[-_]+", "-", candidate).strip("-")
    return ""

def format_display_path(url: str) -> str:
    slug = extract_novel_slug(url)
    parsed = urlparse(url)
    segments = [s for s in parsed.path.strip("/").split("/") if s]
    chapter_part = ""
    if segments and is_chapter_segment(segments[-1]):
        chapter_part = f"/{segments[-1]}"
    display_str = f"{slug}{chapter_part}" if slug else "/".join(segments)
    return display_str[:72] + "..." if len(display_str) > 75 else display_str

def clean_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in STOPWORDS}

def slug_to_display_title(slug: str) -> str:
    if not slug:
        return "Unknown Novel"
    clean = re.sub(r"[-_]+", " ", slug)
    return " ".join(word.capitalize() for word in clean.split())

def are_novels_matching(slug_a: str, slug_b: str) -> bool:
    if not slug_a or not slug_b:
        return False
    if slug_a == slug_b:
        return True

    clean_a = re.sub(r"[-_]+", "-", slug_a.strip().lower()).strip("-")
    clean_b = re.sub(r"[-_]+", "-", slug_b.strip().lower()).strip("-")
    if clean_a == clean_b:
        return True

    nums_a = set(re.findall(r"\b\d+\b", clean_a))
    nums_b = set(re.findall(r"\b\d+\b", clean_b))
    if nums_a != nums_b:
        return False

    tokens_a = clean_tokens(clean_a)
    tokens_b = clean_tokens(clean_b)
    if not tokens_a or not tokens_b:
        return False

    intersection = tokens_a.intersection(tokens_b)
    union = tokens_a.union(tokens_b)
    if tokens_a == tokens_b:
        return True
    return (len(intersection) / len(union)) >= 0.85

def is_channel_named_after_novel(channel_name: str, slug: str) -> bool:
    ch_tokens = clean_tokens(channel_name)
    slug_tokens = clean_tokens(slug)
    if not ch_tokens or not slug_tokens:
        return False
    common = ch_tokens.intersection(slug_tokens)
    return len(common) >= 2 or common == ch_tokens or common == slug_tokens

def extract_target_domain(url: str) -> str | None:
    try:
        hostname = (urlparse(url).hostname or "").lower()
        parts = hostname.split(".")
        for target in TARGET_DOMAINS:
            if target in parts:
                return target
        return None
    except Exception:
        return None

def classify_novel_url(url: str) -> tuple[str, str, float]:
    parsed = urlparse(url)
    clean_path = parsed.path.strip("/")
    parts = [re.sub(r"\.(html|php|htm|aspx)$", "", p.lower()) for p in clean_path.split("/") if p]
    if not parts:
        return ("", "unknown", -1.0)

    has_chapter = False
    chapter_num = -1.0
    for p in parts:
        ch_match = re.search(r'(?:chapter|ch|c)[-_/]?(\d+(?:\.\d+)?)', p)
        if ch_match:
            has_chapter = True
            try:
                chapter_num = float(ch_match.group(1))
            except ValueError:
                pass
            break

    slug = ""
    if has_chapter:
        url_type = "chapter"
        candidate_parts = [p for p in parts if not re.match(r"^(chapter|ch|c|vol|v)[\-_/]?\d+", p) and p not in {"book", "novel"}]
        slug = candidate_parts[-1] if candidate_parts else parts[0]
    else:
        url_type = "book" if any(prefix in parts for prefix in ["book", "novel", "series"]) or len(parts) <= 2 else "unknown"
        slug = parts[-1]

    slug = re.sub(r"[-_]+", "-", slug).strip("-")
    return (slug, url_type, chapter_num)

def normalize_channel_name(title: str, max_length: int = 90) -> str:
    clean = re.sub(r"[^a-zA-Z0-9\s-]", "", title).strip().lower()
    normalized = re.sub(r"[\s_]+", "-", clean)
    return normalized[:max_length].rstrip("-") or "novel-channel"