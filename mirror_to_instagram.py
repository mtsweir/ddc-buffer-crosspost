#!/usr/bin/env python3
"""
Buffer -> Instagram mirror

Takes the most recent SENT Facebook post in Buffer, finds the article link in it,
grabs that article's lead image and headline, builds a branded 1080x1350 image with
the headline on it, uploads that to Cloudinary, and creates a matching Instagram
post through Buffer. Every run is logged to a Google Sheet, which is also used to
avoid posting the same article twice.

Usage:
  python mirror_to_instagram.py              # DRY RUN (default) - shows what would happen, posts nothing
  python mirror_to_instagram.py --live       # actually creates the Instagram post
  python mirror_to_instagram.py --setup      # lists your Buffer organisations and channel IDs
  python mirror_to_instagram.py --inspect    # prints the raw latest Facebook post (for debugging)
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BUFFER_API_URL = "https://api.buffer.com"
USER_AGENT = "Mozilla/5.0 (compatible; BufferInstagramMirror/1.0)"

# Instagram feed limits
IG_CAPTION_MAX = 2200
IG_MAX_HASHTAGS = 30
IG_MIN_RATIO = 0.8    # 4:5 portrait
IG_MAX_RATIO = 1.91   # 1.91:1 landscape
IG_MIN_WIDTH = 320

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SHEET_HEADERS = ["Timestamp (UTC)", "Article URL", "Status", "Note",
                 "Caption", "Image URL", "Mode", "Buffer FB Post ID", "Buffer IG Post ID",
                 "Headline", "Posted Image URL", "Platform"]

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def env(name, required=True, default=None):
    value = os.getenv(name, default)
    if required and not value:
        fail_early(f"Missing setting {name} in your .env file.")
    return value


def fail_early(msg):
    print(f"\n❌ {msg}\n")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Buffer API (GraphQL)
# --------------------------------------------------------------------------- #

def gql_str(value):
    """Safely turn a Python string into a GraphQL string literal."""
    return json.dumps(value, ensure_ascii=False)


def buffer_query(query):
    resp = requests.post(
        BUFFER_API_URL,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {env('BUFFER_API_KEY')}"},
        json={"query": query},
        timeout=30,
    )
    if resp.status_code == 401:
        raise RuntimeError("Buffer rejected the API key (401). Check BUFFER_API_KEY.")
    if resp.status_code == 429:
        raise RuntimeError("Buffer rate limit hit (429). Wait a few minutes and retry.")
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Buffer API error: {data['errors'][0].get('message', data['errors'])}")
    return data["data"]


def list_setup_info():
    data = buffer_query("query { account { organizations { id name } } }")
    orgs = data["account"]["organizations"]
    for org in orgs:
        print(f"\nOrganisation: {org['name']}\n  BUFFER_ORG_ID={org['id']}")
        channels = buffer_query(
            f"query {{ channels(input: {{ organizationId: {gql_str(org['id'])} }}) "
            f"{{ id name displayName service }} }}"
        )["channels"]
        for ch in channels:
            label = ch.get("displayName") or ch.get("name")
            print(f"    {ch['service']:<12} {label:<35} id={ch['id']}")
    print("\nCopy the org ID and the Facebook + Instagram channel IDs into your .env file.\n")


def get_latest_facebook_post():
    query = f"""
    query {{
      posts(
        first: 1
        input: {{
          organizationId: {gql_str(env('BUFFER_ORG_ID'))}
          sort: [{{ field: dueAt, direction: desc }}, {{ field: createdAt, direction: desc }}]
          filter: {{ status: [sent], channelIds: [{gql_str(env('FACEBOOK_CHANNEL_ID'))}] }}
        }}
      ) {{
        edges {{ node {{ id text dueAt createdAt channelId }} }}
      }}
    }}"""
    edges = buffer_query(query)["posts"]["edges"]
    if not edges:
        raise RuntimeError("No sent Facebook posts found in Buffer.")
    return edges[0]["node"]


PLATFORM_NAMES = {"instagram": "Instagram", "tiktok": "TikTok"}
TIKTOK_TITLE_MAX = 90


def trim_words(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:-–— ")
    return cut + "…"


def create_post(platform, caption, image_url, alt_text, headline=""):
    mode = env("POST_MODE", required=False, default="now").lower()
    if mode == "queue":
        scheduling = "mode: addToQueue"
    else:
        delay = int(env("POST_DELAY_MINUTES", required=False, default="2"))
        due = (datetime.now(timezone.utc) + timedelta(minutes=delay)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        scheduling = f"mode: customScheduled\n      dueAt: {gql_str(due)}"

    if platform == "tiktok":
        channel = env("TIKTOK_CHANNEL_ID")
        title = trim_words(headline, TIKTOK_TITLE_MAX) if headline else ""
        metadata = f"metadata: {{ tiktok: {{ title: {gql_str(title)} }} }}" if title else ""
    else:
        channel = env("INSTAGRAM_CHANNEL_ID")
        metadata = "metadata: { instagram: { type: post, shouldShareToFeed: true } }"

    query = f"""
    mutation {{
      createPost(input: {{
        text: {gql_str(caption)}
        channelId: {gql_str(channel)}
        schedulingType: automatic
        {scheduling}
        assets: [{{ image: {{ url: {gql_str(image_url)}, metadata: {{ altText: {gql_str(alt_text)} }} }} }}]
        {metadata}
      }}) {{
        ... on PostActionSuccess {{ post {{ id dueAt }} }}
        ... on MutationError {{ message }}
      }}
    }}"""
    result = buffer_query(query)["createPost"]
    if "message" in result and "post" not in result:
        raise RuntimeError(f"Buffer refused the {PLATFORM_NAMES[platform]} post: {result['message']}")
    return result["post"]


# --------------------------------------------------------------------------- #
# Article + image
# --------------------------------------------------------------------------- #

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def extract_article_url(text):
    urls = URL_RE.findall(text or "")
    if not urls:
        return None
    return urls[0].rstrip(".,;:!?")


def normalise_url(url):
    """Strip tracking params / fragments / trailing slash so duplicates match."""
    p = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(p.query)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(query), ""))


def find_og_image(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    for attr, name in (("property", "og:image:secure_url"), ("property", "og:image"),
                       ("name", "og:image"), ("name", "twitter:image"),
                       ("property", "twitter:image")):
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content"):
            return requests.compat.urljoin(page_url, tag["content"].strip())
    return None


def find_headline(html):
    soup = BeautifulSoup(html, "html.parser")
    # The article's on-page headline (<h1>) is the cleanest source: no site-name suffix.
    h1 = soup.find("h1")
    if h1:
        heading = " ".join(h1.get_text(" ", strip=True).split())
        if 10 <= len(heading) <= 200:
            return heading
    tag = (soup.find("meta", attrs={"property": "og:title"})
           or soup.find("meta", attrs={"name": "twitter:title"}))
    title = tag["content"].strip() if tag and tag.get("content") else (
        soup.title.get_text(strip=True) if soup.title else "")
    site = soup.find("meta", attrs={"property": "og:site_name"})
    names = {"Drugs.com"} | ({site["content"].strip()} if site and site.get("content") else set())
    for name in names:  # drop trailing " - Drugs.com", " | Drugs.com MedNews", etc.
        title = re.sub(rf"\s+[-|–—:]\s+{re.escape(name)}.*$", "", title, flags=re.I)
    return title.strip()


def fetch_article(article_url):
    """Return (image_url, final_url, headline) for an article link."""
    resp = requests.get(article_url, headers={"User-Agent": USER_AGENT},
                        timeout=20, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"Article link returned HTTP {resp.status_code}: {article_url}")
    image_url = find_og_image(resp.text, resp.url)
    if not image_url:
        raise RuntimeError("No og:image found on the article page - add an image manually.")
    return image_url, resp.url, find_headline(resp.text)


def check_image(image_url):
    """Return (width, height, warnings, image_bytes). Raises if the image is unusable."""
    resp = requests.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Image URL returned HTTP {resp.status_code}: {image_url}")
    ctype = resp.headers.get("Content-Type", "")
    if not ctype.startswith("image/"):
        raise RuntimeError(f"Image URL isn't an image (Content-Type: {ctype or 'unknown'}).")

    warnings = []
    if "jpeg" not in ctype and "jpg" not in ctype:
        warnings.append(f"Image is {ctype}, Instagram prefers JPEG - Buffer may convert it, "
                        f"but check the first live post.")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(resp.content)) as img:
            width, height = img.size
    except Exception:
        return None, None, warnings + ["Couldn't read image dimensions."], resp.content

    ratio = width / height
    if width < IG_MIN_WIDTH:
        warnings.append(f"Image is only {width}px wide - may look soft on Instagram.")
    if not (IG_MIN_RATIO <= ratio <= IG_MAX_RATIO):
        warnings.append(f"Aspect ratio {ratio:.2f}:1 is outside Instagram's 0.8-1.91 range - "
                        f"Instagram may reject or crop it.")
    return width, height, warnings, resp.content


# --------------------------------------------------------------------------- #
# Branded image (headline overlay)
# --------------------------------------------------------------------------- #

IG_W, IG_H = 1080, 1350           # Instagram 4:5 portrait
TIKTOK_W, TIKTOK_H = 1080, 1920   # TikTok 9:16 full screen

FONT_FALLBACKS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",            # macOS
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",         # Linux
    "C:/Windows/Fonts/arialbd.ttf",                                 # Windows
]


def hex_colour(value, default):
    value = (value or default).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def resolve_path(path):
    return path if os.path.isabs(path) else os.path.join(SCRIPT_DIR, path)


def load_font(size, setting="FONT_FILE"):
    from PIL import ImageFont
    custom = env(setting, required=False, default="")
    candidates = ([resolve_path(custom)] if custom else []) + FONT_FALLBACKS
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def split_long_word(draw, word, font, max_width):
    """Break a word too wide for one line: at hyphens/slashes first, then anywhere."""
    if draw.textlength(word, font=font) <= max_width:
        return [word]
    pieces = [p for p in re.split(r"(?<=[-/])", word) if p]
    out, current = [], ""
    for piece in pieces:
        if draw.textlength(current + piece, font=font) <= max_width:
            current += piece
            continue
        if current:
            out.append(current)
        current = ""
        for ch in piece:  # last resort: hard break
            if draw.textlength(current + ch, font=font) > max_width and current:
                out.append(current)
                current = ""
            current += ch
    if current:
        out.append(current)
    return out


def wrap_text(draw, text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        for part in split_long_word(draw, word, font, max_width):
            trial = f"{line} {part}".strip() if not line.endswith(("-", "/")) else line + part
            if line and draw.textlength(trial, font=font) > max_width:
                lines.append(line)
                line = part
            else:
                line = trial
    if line:
        lines.append(line)
    return lines


def fit_headline(draw, text, box_w, box_h, max_size=76, min_size=44):
    """Largest font size at which the headline fits the box; truncates if it never does."""
    for size in range(max_size, min_size - 1, -2):
        font = load_font(size)
        lines = wrap_text(draw, text, font, box_w)
        line_h = int(size * 1.18)
        if len(lines) * line_h <= box_h:
            return font, lines, line_h
    font = load_font(min_size)
    line_h = int(min_size * 1.18)
    lines = wrap_text(draw, text, font, box_w)[:max(1, box_h // line_h)]
    last = lines[-1].rstrip(".,;: ")
    while last and draw.textlength(last + "…", font=font) > box_w:
        last = last[:-1].rstrip()
    lines[-1] = last + "…"
    return font, lines, line_h


def cover_crop(img, w, h):
    """Scale and centre-crop img to exactly w x h."""
    from PIL import Image
    scale = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def build_branded_image(image_bytes, headline, platform="instagram"):
    """Return JPEG bytes of the branded image: 1080x1350 for Instagram, 1080x1920 for TikTok."""
    from PIL import Image, ImageDraw, ImageFilter

    if platform == "tiktok":
        CANVAS_W, CANVAS_H = TIKTOK_W, TIKTOK_H
        layout = "overlay"
        frame = env("TIKTOK_FRAME_PNG", required=False, default="")
        box = env("TIKTOK_HEADLINE_BOX", required=False, default="60,1080,840,400")
    else:
        CANVAS_W, CANVAS_H = IG_W, IG_H
        layout = env("IMAGE_LAYOUT", required=False, default="split").lower()
        frame = env("FRAME_PNG", required=False, default="")
        box = env("HEADLINE_BOX", required=False, default="")
    band = hex_colour(env("BRAND_COLOUR", required=False), "0B2545")
    text_colour = hex_colour(env("TEXT_COLOUR", required=False), "FFFFFF")
    accent = hex_colour(env("ACCENT_COLOUR", required=False), "8EE3EF")
    label = env("IMAGE_LABEL", required=False, default="")
    margin = 72
    box_x, box_w = margin, CANVAS_W - 2 * margin

    src = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), band)

    fit = env("TIKTOK_PHOTO_FIT", required=False, default="cover").lower()
    if layout == "overlay" and platform == "tiktok" and fit == "blur":
        # Whole photo across the middle, blurred enlarged copy filling the rest.
        background = cover_crop(src, CANVAS_W, CANVAS_H).filter(ImageFilter.GaussianBlur(40))
        canvas.paste(background, (0, 0))
        photo_h = min(round(CANVAS_W * src.height / src.width), int(CANVAS_H * 0.5))
        top = int(env("TIKTOK_PHOTO_TOP", required=False, default="300"))
        canvas.paste(cover_crop(src, CANVAS_W, photo_h), (0, top))
    elif layout == "overlay":
        # Photo fills the frame; dark gradient at the bottom for the text
        # (skipped when a Canva frame provides its own gradient).
        canvas.paste(cover_crop(src, CANVAS_W, CANVAS_H), (0, 0))
    if layout == "overlay" and not frame:
        grad_top = int(CANVAS_H * 0.45)
        gradient = Image.new("L", (1, CANVAS_H - grad_top))
        for y in range(gradient.height):
            gradient.putpixel((0, y), int(235 * (y / gradient.height) ** 1.3))
        shade = Image.new("RGB", (CANVAS_W, CANVAS_H - grad_top), (0, 0, 0))
        canvas.paste(shade, (0, grad_top), gradient.resize((CANVAS_W, gradient.height)))
    if layout == "overlay":
        text_top, text_bottom = int(CANVAS_H * 0.62), CANVAS_H - margin
    else:
        # "split": photo across the top, solid brand band below for the text.
        photo_h = min(round(CANVAS_W * src.height / src.width), int(CANVAS_H * 0.6))
        canvas.paste(cover_crop(src, CANVAS_W, photo_h), (0, 0))
        text_top, text_bottom = photo_h + margin, CANVAS_H - margin

    if frame:
        frame_path = resolve_path(frame)
        if not os.path.exists(frame_path):
            raise RuntimeError(f"Frame image not found: {frame_path}")
        overlay = Image.open(frame_path).convert("RGBA")
        if overlay.size != (CANVAS_W, CANVAS_H):
            raise RuntimeError(f"{os.path.basename(frame_path)} is {overlay.size[0]}x{overlay.size[1]}, "
                               f"expected {CANVAS_W}x{CANVAS_H}.")
        canvas.paste(overlay, (0, 0), overlay)
    if frame or platform == "tiktok":
        if box:  # "x,y,width,height" in pixels on the canvas
            x, y, w, h = (int(v) for v in box.split(","))
            box_x, box_w, text_top, text_bottom = x, w, y, y + h

    draw = ImageDraw.Draw(canvas)
    label_size, label_gap = 30, 22
    label_h = (label_size + label_gap) if label else 0
    font, lines, line_h = fit_headline(draw, headline, box_w, text_bottom - text_top - label_h)
    block_h = label_h + len(lines) * line_h
    # overlay: block sits at the bottom of the box; split: at the top of the band
    y0 = text_bottom - block_h if layout == "overlay" else text_top

    items, y = [], y0
    if label:
        items.append((y, label.upper(), load_font(label_size), accent))
        y += label_h
    for line in lines:
        items.append((y, line, font, text_colour))
        y += line_h

    # Soft shadow behind the text so it stays readable on bright photos.
    shadow_strength = int(env("TEXT_SHADOW", required=False, default="160"))
    if shadow_strength > 0:
        from PIL import ImageFilter
        shadow = Image.new("L", canvas.size, 0)
        sdraw = ImageDraw.Draw(shadow)
        for ty, text, f, _ in items:
            sdraw.text((box_x + 2, ty + 3), text, font=f, fill=shadow_strength)
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        canvas.paste(Image.new("RGB", canvas.size, (0, 0, 0)), (0, 0), shadow)

    for ty, text, f, colour in items:
        draw.text((box_x, ty), text, font=f, fill=colour)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def slugify(url):
    path = urlparse(url).path.rstrip("/").split("/")[-1] or "post"
    return re.sub(r"[^a-z0-9-]+", "-", path.lower().replace(".html", ""))[:80].strip("-")


def save_preview(jpeg_bytes, slug):
    folder = os.path.join(SCRIPT_DIR, "previews")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{slug}.jpg")
    with open(path, "wb") as f:
        f.write(jpeg_bytes)
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)  # pops it open in Preview
    return path


def upload_to_cloudinary(jpeg_bytes, slug):
    import cloudinary
    import cloudinary.uploader
    env("CLOUDINARY_URL")  # SDK reads it from the environment
    cloudinary.config(secure=True)
    folder = env("CLOUDINARY_FOLDER", required=False, default="instagram-mirror")
    result = cloudinary.uploader.upload(io.BytesIO(jpeg_bytes), folder=folder,
                                        public_id=slug, overwrite=True,
                                        resource_type="image")
    url = result.get("secure_url")
    if not url:
        raise RuntimeError(f"Cloudinary upload returned no URL: {result}")
    check = requests.head(url, timeout=20)
    if check.status_code >= 400:
        raise RuntimeError(f"Uploaded image isn't publicly reachable (HTTP {check.status_code}).")
    return url


# --------------------------------------------------------------------------- #
# Caption
# --------------------------------------------------------------------------- #

def build_caption(fb_text, article_url):
    # Links aren't clickable in Instagram captions, so drop them.
    caption = URL_RE.sub("", fb_text or "")
    # Drop phrases left dangling once the link is gone, e.g. "Read more:" / "Full story ->"
    caption = re.sub(r"(?im)[ \t]*(read more|read the full (story|article)|full (story|article)|"
                     r"more here|link|learn more)\s*[:\-–—>→👉]*\s*$", "", caption)
    caption = re.sub(r"[ \t]{2,}", " ", caption)
    caption = re.sub(r"[ \t]+\n", "\n", caption)
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()

    suffix = env("INSTAGRAM_CAPTION_SUFFIX", required=False, default="").replace("\\n", "\n")
    if suffix:
        caption = f"{caption}\n\n{suffix}"

    warnings = []
    if len(caption) > IG_CAPTION_MAX:
        caption = caption[:IG_CAPTION_MAX - 1].rstrip() + "…"
        warnings.append("Caption was over 2,200 characters and was trimmed.")
    if len(re.findall(r"#\w+", caption)) > IG_MAX_HASHTAGS:
        warnings.append("Caption has more than 30 hashtags - Instagram will reject it.")
    if not caption:
        raise RuntimeError("Caption is empty after removing the link.")
    return caption, warnings


# --------------------------------------------------------------------------- #
# Google Sheet log
# --------------------------------------------------------------------------- #

class SheetLog:
    def __init__(self):
        import gspread
        creds_path = env("GOOGLE_CREDENTIALS_FILE")
        if not os.path.isabs(creds_path):
            creds_path = os.path.join(SCRIPT_DIR, creds_path)
        if not os.path.exists(creds_path):
            fail_early(f"Google credentials file not found at: {creds_path}")
        gc = gspread.service_account(filename=creds_path)
        sh = gc.open_by_key(env("GOOGLE_SHEET_ID"))
        tab = env("GOOGLE_SHEET_TAB", required=False, default="Log")
        try:
            self.ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            self.ws = sh.add_worksheet(title=tab, rows=1000, cols=len(SHEET_HEADERS))
        if self.ws.row_values(1) != SHEET_HEADERS:
            self.ws.update(range_name="A1", values=[SHEET_HEADERS])
            self.ws.freeze(rows=1)

    def load(self):
        self.rows = self.ws.get_all_records(expected_headers=SHEET_HEADERS)

    def already_posted(self, norm_url, platform):
        for row in getattr(self, "rows", []):
            row_platform = str(row.get("Platform") or "instagram").lower()  # old rows = Instagram
            if (row.get("Status") == "POSTED" and row_platform == platform
                    and normalise_url(str(row.get("Article URL", ""))) == norm_url):
                return row.get("Timestamp (UTC)", "earlier")
        return None

    def write(self, **fields):
        fields.setdefault("Timestamp (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        self.ws.append_row([str(fields.get(h, "")) for h in SHEET_HEADERS],
                           value_input_option="RAW")


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def enabled_platforms(only=None):
    if only:
        return [only]
    configured = env("PLATFORMS", required=False, default="")
    if configured:
        return [p.strip().lower() for p in configured.split(",") if p.strip()]
    platforms = ["instagram"]
    if env("TIKTOK_CHANNEL_ID", required=False):
        platforms.append("tiktok")
    return platforms


def run(live, only=None):
    mode_label = "LIVE" if live else "DRY RUN"
    platforms = enabled_platforms(only)
    for p in platforms:
        if p not in PLATFORM_NAMES:
            fail_early(f"Unknown platform '{p}' in PLATFORMS (use instagram and/or tiktok).")
    names = " + ".join(PLATFORM_NAMES[p] for p in platforms)
    print(f"\n=== Buffer → {names} ({mode_label}) ===\n")

    log = SheetLog()
    base = {"Mode": mode_label}
    icons = {"POSTED": "✅", "DRY_RUN_OK": "🧪", "SKIPPED_DUPLICATE": "⏭️"}

    def record(row, status, note=""):
        row.update(Status=status, Note=note)
        try:
            log.write(**row)
        except Exception as e:  # never hide the real result because logging failed
            print(f"⚠️  Couldn't write to Google Sheet: {e}")
        label = PLATFORM_NAMES.get(row.get("Platform", ""), "")
        print(f"{icons.get(status, '❌')} {label + ': ' if label else ''}{status}"
              f"{' - ' + note if note else ''}")
        return status

    # ---- Shared steps: Facebook post, article, image, headline, caption
    try:
        log.load()
        post = get_latest_facebook_post()
        base["Buffer FB Post ID"] = post["id"]
        print(f"Latest Facebook post ({post.get('dueAt') or post.get('createdAt')}):\n"
              f"  {post['text'][:200]}{'…' if len(post['text']) > 200 else ''}\n")

        article_url = extract_article_url(post["text"])
        if not article_url:
            raise RuntimeError("No article link found in the Facebook post text.")
        base["Article URL"] = article_url
        print(f"Article:  {article_url}")

        source_image_url, final_url, headline = fetch_article(article_url)
        article_url = normalise_url(final_url)
        base["Article URL"] = article_url
        print(f"Resolved: {article_url}")

        base["Image URL"] = source_image_url
        width, height, img_warnings, image_bytes = check_image(source_image_url)
        print(f"Image:    {source_image_url}" + (f"  ({width}×{height})" if width else ""))

        branded = env("IMAGE_STYLE", required=False, default="branded").lower() == "branded"
        if branded or "tiktok" in platforms:
            if not headline:
                raise RuntimeError("No headline found on the article page.")
            base["Headline"] = headline
            print(f"Headline: {headline}")
        if branded:
            # Branded images have a fixed shape, so the source shape no longer matters.
            img_warnings = [w for w in img_warnings if not w.startswith("Aspect ratio")]

        caption, cap_warnings = build_caption(post["text"], article_url)
        base["Caption"] = caption
        print(f"\nCaption:\n{'-' * 40}\n{caption}\n{'-' * 40}")
        shared_warnings = img_warnings + cap_warnings
        for w in shared_warnings:
            print(f"⚠️  {w}")
    except requests.RequestException as e:
        record(dict(base), "FAILED", f"Network problem: {e}")
        sys.exit(1)
    except Exception as e:
        record(dict(base), "FAILED", str(e))
        sys.exit(1)

    # ---- Per platform: duplicate check, image, upload, post
    results = []
    slug = slugify(article_url)
    for platform in platforms:
        name = PLATFORM_NAMES[platform]
        row = dict(base, Platform=platform)
        print(f"\n--- {name} ---")
        try:
            warnings = list(shared_warnings)
            seen = log.already_posted(article_url, platform)
            if seen and live:
                results.append(record(row, "SKIPPED_DUPLICATE", f"Already posted to {name} on {seen}."))
                continue
            if seen:
                warnings.insert(0, f"Already posted to {name} on {seen} - a live run would skip it.")

            # TikTok always gets a branded 9:16 image (the article image is landscape).
            use_branded = branded or platform == "tiktok"
            if use_branded:
                jpeg = build_branded_image(image_bytes, headline, platform)
                preview = save_preview(jpeg, f"{slug}-{platform}")
                print(f"Preview:  {preview}")
            if platform == "tiktok":
                print(f"Title:    {trim_words(headline, TIKTOK_TITLE_MAX)}")

            note = " | ".join(warnings)
            if not live:
                results.append(record(row, "DRY_RUN_OK", note or "Everything checks out. Re-run with --live to post."))
                continue

            if use_branded:
                public_id = slug if platform == "instagram" else f"{slug}-{platform}"
                post_image_url = upload_to_cloudinary(jpeg, public_id)
                print(f"Uploaded: {post_image_url}")
            else:
                post_image_url = source_image_url
            row["Posted Image URL"] = post_image_url

            alt_text = env("IMAGE_ALT_TEXT", required=False, default="") or (
                headline if use_branded else caption.split("\n")[0])[:250]
            new_post = create_post(platform, caption, post_image_url, alt_text, headline)
            row["Buffer IG Post ID"] = new_post.get("id", "")
            results.append(record(row, "POSTED",
                                  (f"Scheduled in Buffer for {new_post.get('dueAt')}. " + note).strip()))
        except requests.RequestException as e:
            results.append(record(row, "FAILED", f"Network problem: {e}"))
        except Exception as e:
            results.append(record(row, "FAILED", str(e)))

    print()
    sys.exit(1 if "FAILED" in results else 0)


def main():
    parser = argparse.ArgumentParser(description="Mirror the latest Buffer Facebook post to Instagram.")
    parser.add_argument("--live", action="store_true", help="Actually create the Instagram post.")
    parser.add_argument("--setup", action="store_true", help="List Buffer org and channel IDs.")
    parser.add_argument("--inspect", action="store_true", help="Print the raw latest Facebook post.")
    parser.add_argument("--only", choices=sorted(PLATFORM_NAMES), help="Post to just one platform this run.")
    args = parser.parse_args()

    if args.setup:
        list_setup_info()
    elif args.inspect:
        print(json.dumps(get_latest_facebook_post(), indent=2, ensure_ascii=False))
    else:
        run(live=args.live, only=args.only)


if __name__ == "__main__":
    main()
