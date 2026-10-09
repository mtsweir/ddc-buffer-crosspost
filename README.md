# Buffer → Instagram Mirror — Setup Guide

When the editor's Facebook post goes out through Buffer, you run this script. It takes the latest sent Facebook post, pulls the article link out of it, grabs that article's lead image, and creates a matching Instagram post — through Buffer itself. Every run is logged to a Google Sheet, which also stops the same article being posted twice.

## How it works (and one change from our plan)

Buffer's current API can create Instagram posts directly, so the script never talks to Meta/Instagram itself. That means no Facebook developer app, no Graph API, and no 60-day Instagram token to keep refreshing — Buffer handles the Instagram connection you've already set up in its dashboard.

The trade-off is that Buffer needs images as a **public URL**. The article's `og:image` already is one, so the script passes it straight through rather than padding it to a square (padding would mean hosting a new image somewhere). Since your images are illustrative and you're fine with cropping, this is the simplest route. The script still checks the shape and warns you if an image falls outside Instagram's allowed range (4:5 portrait to 1.91:1 landscape). Standard article images (around 1.91:1) fit.

```
Editor posts to Facebook via Buffer
        │  (Buffer notification on your phone)
        ▼
You run the script ──► reads latest SENT Facebook post from Buffer
                       ├─ finds the article link in the post
                       ├─ checks Google Sheet: already posted? → skip
                       ├─ fetches the article's og:image
                       ├─ builds caption (summary, link removed)
                       └─ creates the Instagram post in Buffer → logs result to Sheet
```

## What you need

- Python 3.9 or newer on whatever computer will run it
- Your Buffer account with **both** the Facebook page and the Instagram business account connected as channels
- A Google account for the log sheet

## Step 1 — Install

```bash
cd buffer-to-instagram
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

## Step 2 — Buffer API key

1. Go to **publish.buffer.com/settings/api** and create a personal API key.
2. Paste it into `.env` as `BUFFER_API_KEY`.
3. Run `python mirror_to_instagram.py --setup`. It lists your organisation and every channel with its ID.
4. Copy the org ID, the **Facebook** channel ID and the **Instagram** channel ID into `.env`.

Treat the API key like a password. If it ever leaks, delete it in Buffer and make a new one.

## Step 3 — Google Sheet log

1. Go to **console.cloud.google.com**, create a project (e.g. "Buffer Instagram Mirror").
2. Enable two APIs: **Google Sheets API** and **Google Drive API**.
3. Go to *IAM & Admin → Service Accounts*, create a service account, then under *Keys* add a JSON key. A file downloads.
4. Rename that file `service-account.json` and put it in the project folder.
5. Create a new Google Sheet. Click **Share** and share it with the service account's email (it looks like `name@project.iam.gserviceaccount.com`) as **Editor**.
6. Copy the sheet ID from its URL — the long string between `/d/` and `/edit` — into `.env` as `GOOGLE_SHEET_ID`.

The script creates a `Log` tab with headers on first run.

## Step 4 — Test with a dry run

```bash
python mirror_to_instagram.py
```

Dry run is the **default**. It does everything — reads Buffer, finds the link, fetches and checks the image, builds the caption, checks for duplicates — then stops before posting and shows you what it would have sent. It's also logged in the sheet as `DRY_RUN_OK`.

If it can't find the article link, run `python mirror_to_instagram.py --inspect` to see exactly what Buffer returns for the latest Facebook post, and send me the output (remove anything sensitive first).

## Step 5 — Go live

```bash
python mirror_to_instagram.py --live
```

With `POST_MODE=now` the Instagram post is scheduled about two minutes ahead, so it appears in your Buffer Instagram queue first — handy for a last glance. Set `POST_MODE=queue` if you'd rather it drop into the next open Instagram slot.

## Branded images (headline overlay)

By default the script no longer posts the article image as is. It builds a 1080×1350 image (Instagram's 4:5 portrait size) with the article's headline on it, so the post makes sense to someone scrolling past.

1. The headline is taken from the article page's `og:title`, the editor's approved headline word for word, with the trailing " - Drugs.com" removed. The script never rewrites it.
2. The image is built on your Mac. In a dry run it's saved to `previews/` and opened in Preview, so you can see exactly what would be posted.
3. On a live run it's uploaded to Cloudinary (Buffer needs a public image link), and that link is sent to Buffer.

**Setup:** in Cloudinary, go to *Dashboard → API Keys* and copy the **API environment variable** (it starts with `cloudinary://`). Paste it into `.env` as `CLOUDINARY_URL=`. Then run `pip install -r requirements.txt` again to add the Cloudinary library.

**Design settings** (all in `.env`):

| Setting | What it does |
|---|---|
| `IMAGE_STYLE` | `branded` (default) or `original` to go back to posting the article image as is |
| `IMAGE_LAYOUT` | `split`: photo on top, colour band with headline below (whole photo stays visible). `overlay`: photo fills the image, headline on a dark gradient at the bottom |
| `IMAGE_LABEL` | Small label above the headline, e.g. `Health News`. Leave blank for none |
| `BRAND_COLOUR`, `TEXT_COLOUR`, `ACCENT_COLOUR` | Band colour, headline colour, label colour (hex) |
| `FONT_FILE` | A `.ttf` or `.otf` file in the project folder. Without it, Arial Bold is used |
| `FRAME_PNG` | A 1080×1350 transparent PNG exported from Canva, laid on top of the image (logo, borders, etc.) |
| `HEADLINE_BOX` | With a frame: where the headline goes, as `x,y,width,height` in pixels |
| `TEXT_SHADOW` | Soft shadow behind the text for readability on bright photos, 0 (off) to 255. Default 160 |

**Using a Canva frame:** create a 1080×1350 design in Canva with your logo and branding, leaving the photo area and headline area transparent. Download it as PNG with *Transparent background* ticked, put it in the project folder, and set `FRAME_PNG=frame.png`. Run a dry run to check the result. Note that fonts used inside Canva aren't automatically licensed for other use; check before adding one as `FONT_FILE`.

Long headlines shrink automatically to fit. Extremely long ones are trimmed with "…", which you'll see in the dry-run preview.

## TikTok

If `TIKTOK_CHANNEL_ID` is set in `.env`, every run posts to Instagram and TikTok. Each platform is handled separately: its own 1080×1920 branded image (built with `frame-tiktok.png`), its own Cloudinary upload, its own row in the sheet, and its own duplicate check. If one platform fails, the other still posts, and the next run only retries the one that failed.

- `python mirror_to_instagram.py --live --only tiktok` posts to just one platform for this run (`--only instagram` works too).
- The TikTok post gets the headline as its title (TikTok allows 90 characters, so longer headlines are trimmed at a word) and the same caption as Instagram.
- `TIKTOK_PHOTO_FIT=cover` fills the screen with the photo; `blur` shows the whole photo across the middle with a blurred copy behind it, which suits landscape article images better.
- The TikTok frame must be exactly 1080×1920, and the Instagram frame exactly 1080×1350; the script stops with a clear message if either is the wrong size.

## The log sheet

| Column | What it shows |
|---|---|
| Timestamp (UTC) | When the run happened |
| Article URL | The link found in the Facebook post (the duplicate key) |
| Status | `POSTED`, `DRY_RUN_OK`, `SKIPPED_DUPLICATE` or `FAILED` |
| Note | Why it failed, or any warnings (image shape, long caption) |
| Caption | Exactly what went (or would go) to Instagram |
| Image URL | The original article image |
| Mode | LIVE or DRY RUN |
| Buffer FB / IG Post ID | For tracing a post back in Buffer (the IG column holds the TikTok post ID on TikTok rows) |
| Headline | The headline written on the image |
| Posted Image URL | The Cloudinary image that was actually posted |
| Platform | instagram or tiktok (older rows without it count as Instagram) |

Only `POSTED` rows count as duplicates, so dry runs and failures never block a real post. Tracking parameters like `utm_source` are ignored when matching, so the same article with different tracking still counts as a duplicate.

## Edge cases it handles

- **No image on the article / broken link** → fails cleanly with a clear reason and logs `FAILED`. Post that one by hand.
- **Image an odd shape or tiny** → warns you; still posts, since Instagram usually accepts or crops it. If Instagram rejects it, Buffer will show the error on that post.
- **Links in the caption** → removed (they aren't clickable on Instagram), along with dangling "Read more:" phrases. `INSTAGRAM_CAPTION_SUFFIX` adds a line such as "Full story at the link in our bio."
- **Caption over 2,200 characters** → trimmed, with a warning. More than 30 hashtags → warning.
- **Running it twice** → the second run logs `SKIPPED_DUPLICATE` and does nothing.
- **Instagram disconnected in Buffer** → Buffer returns an error, which the script reports. Reconnect the channel in Buffer's dashboard.

## Housekeeping

- Before going live, tune the Buffer app's phone notifications to just "post published" alerts, so every ping means a real post.
- Never share or commit `.env` or `service-account.json` (the included `.gitignore` already excludes them).
- Buffer's API is newer and still evolving. If a run suddenly fails with an API error after weeks of working, check developers.buffer.com/changelog.html — the fix is usually a small field change.

## Possible next steps

- **Trigger from your phone:** put the script in a private GitHub repo and run it with a GitHub Actions "Run workflow" button, which works from the GitHub mobile app. No computer needs to be on.
- **TikTok:** TikTok isn't in the platform list in Buffer's API guide right now, so check Buffer's docs again when we get to it; otherwise it would need TikTok's own posting API.
