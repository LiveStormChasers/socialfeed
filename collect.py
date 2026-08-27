#!/usr/bin/env python3
"""
Social feed collector — runs inside GitHub Actions, writes feed.json.

Fetch routes, tried in order per platform:

  X          1. syndication.twitter.com timeline-profile page (__NEXT_DATA__ JSON)
             2. cdn.syndication.twimg.com timeline JSON
             3. logged-in browser, only if the X_COOKIES secret is set
  Facebook   1. logged-out page fetch, posts read from the embedded JSON blobs
             2. logged-out browser, posts read from the rendered DOM
             3. logged-in browser, only if the FB_COOKIES secret is set

Nothing here needs an account: public Facebook Pages render for logged-out
visitors, and X's syndication endpoints are the ones that serve embedded
timelines on third-party sites. All of them are unofficial and can change
without notice, which is why every source records why it failed instead of
silently producing an empty feed.

Local use:
    python collect.py                 normal run
    python collect.py --only x        one platform
    python collect.py --verbose       show each attempt
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "sources.json"
FEED_PATH = BASE / "feed.json"
MEDIA_DIR = BASE / "media"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

SYNDICATION_PAGE = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
SYNDICATION_CDN = "https://cdn.syndication.twimg.com/timeline/profile"

VERBOSE = False
ID_PARAMS = ("story_fbid", "fbid", "id", "v")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {msg}", flush=True)


def vlog(msg):
    if VERBOSE:
        log("    " + msg)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def canonical_url(url):
    """Strip tracking params but keep the ones that identify the post.

    Facebook's permalink.php / story.php URLs carry the post id in the query
    string, so a blanket strip would collapse every such post into one entry.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    keep = ""
    if parts.query and re.search(r"/(permalink|story|photo|video|watch)\.php$", parts.path):
        pairs = [kv for kv in parts.query.split("&") if kv.split("=")[0] in ID_PARAMS]
        keep = "&".join(sorted(pairs))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), keep, ""))


def post_id(url, text, platform, handle):
    key = canonical_url(url) if url else f"{platform}:{handle}:{(text or '')[:180]}"
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def parse_twitter_date(raw):
    if not raw:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Facebook relative-time parsing (only used on the cookie route)
# --------------------------------------------------------------------------

REL_RE = re.compile(r"^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)\b", re.I)
ABS_RE = re.compile(
    r"([A-Z][a-z]{2,8})\s+(\d{1,2})(?:,\s*(\d{4}))?(?:\s+at\s+(\d{1,2}):(\d{2})\s*([AP]M))?", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())})


def parse_loose_time(raw):
    if not raw:
        return None
    s = raw.strip()
    ref = now_utc()
    if re.match(r"^\s*(just now|now)\s*$", s, re.I):
        return ref
    m = REL_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("m"):
            return ref - timedelta(minutes=n)
        if unit.startswith("h"):
            return ref - timedelta(hours=n)
        if unit.startswith("d"):
            return ref - timedelta(days=n)
        if unit.startswith("w"):
            return ref - timedelta(weeks=n)
    if re.match(r"^\s*yesterday", s, re.I):
        base = ref - timedelta(days=1)
        tm = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", s, re.I)
        if tm:
            hour = int(tm.group(1)) % 12 + (12 if tm.group(3).upper() == "PM" else 0)
            return base.replace(hour=hour, minute=int(tm.group(2)), second=0, microsecond=0)
        return base
    m = ABS_RE.search(s)
    if m and m.group(1).lower() in MONTHS:
        month, day = MONTHS[m.group(1).lower()], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else ref.year
        hour, minute = 12, 0
        if m.group(4):
            hour = int(m.group(4)) % 12 + (12 if (m.group(6) or "AM").upper() == "PM" else 0)
            minute = int(m.group(5))
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            return None
        if not m.group(3) and dt > ref + timedelta(days=1):
            dt = dt.replace(year=year - 1)
        return dt
    return None


# --------------------------------------------------------------------------
# X: syndication routes
# --------------------------------------------------------------------------

def syndication_token(handle):
    """The CDN endpoint wants a token derived from the account. Any stable
    value works in practice; this mirrors the widget's own derivation."""
    n = int(hashlib.sha1(handle.lower().encode()).hexdigest()[:12], 16)
    return re.sub(r"[0.]", "", format(n / 1e15 * 3.141592653589793, "f"))[:14] or "0"


MAX_WALK_NODES = 400000


def walk_json(root):
    """Breadth-first iteration over every node in a JSON tree.

    Iterative on purpose. These payloads nest far deeper than is comfortable
    for recursion — Facebook's runs past twenty levels before reaching a post —
    and a depth cap tuned by eye silently drops the real data.
    """
    queue = deque([root])
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        if seen > MAX_WALK_NODES:
            return
        yield node
        if isinstance(node, dict):
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)


def harvest_tweets(node, found=None):
    """Pull out anything shaped like a tweet, wherever it sits.

    Structural rather than path-based: the syndication payload has been
    reshaped several times, but a tweet has always been an object carrying
    id_str + created_at + some text field.
    """
    if found is None:
        found = {}
    for n in walk_json(node):
        if not isinstance(n, dict):
            continue
        text = n.get("full_text") or n.get("text")
        if isinstance(n.get("id_str"), str) and n.get("created_at") and isinstance(text, str):
            found.setdefault(n["id_str"], n)
    return found


def tweet_media(tw):
    """Collect image URLs and video info from any of the shapes seen in the wild."""
    images, has_video, video_url = [], False, ""

    def add(u):
        if u and isinstance(u, str) and u not in images:
            images.append(re.sub(r"name=[^&]+", "name=large", u))

    for p in tw.get("photos") or []:
        if isinstance(p, dict):
            add(p.get("url") or p.get("media_url_https"))

    for m in (tw.get("mediaDetails") or []):
        if not isinstance(m, dict):
            continue
        if m.get("type") in ("video", "animated_gif"):
            has_video = True
            variants = ((m.get("video_info") or {}).get("variants")) or []
            mp4 = [v for v in variants if isinstance(v, dict) and v.get("content_type") == "video/mp4"]
            if mp4:
                best = max(mp4, key=lambda v: v.get("bitrate") or 0)
                video_url = best.get("url", "")
        add(m.get("media_url_https"))

    vid = tw.get("video")
    if isinstance(vid, dict):
        has_video = True
        add(vid.get("poster"))
        for v in vid.get("variants") or []:
            if isinstance(v, dict) and v.get("type") == "video/mp4" and not video_url:
                video_url = v.get("src", "")

    ext = ((tw.get("entities") or {}).get("media")) or []
    for m in ext:
        if isinstance(m, dict):
            add(m.get("media_url_https"))
            if m.get("type") in ("video", "animated_gif"):
                has_video = True

    card = tw.get("card") or {}
    binding = (card.get("binding_values") or {}) if isinstance(card, dict) else {}
    if isinstance(binding, dict):
        for key in ("thumbnail_image_large", "photo_image_full_size_large", "summary_photo_image"):
            val = binding.get(key)
            if isinstance(val, dict):
                add(((val.get("image_value") or {}).get("url")))

    return images[:4], has_video, video_url


def normalise_tweet(tw, handle, label):
    user = tw.get("user") or {}
    author = user.get("screen_name") or handle
    tid = tw.get("id_str") or ""
    url = f"https://x.com/{author}/status/{tid}" if tid else ""
    text = (tw.get("full_text") or tw.get("text") or "").strip()
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text)
    ts = parse_twitter_date(tw.get("created_at"))
    images, has_video, video_url = tweet_media(tw)
    return {
        "id": post_id(url, text, "x", handle),
        "platform": "x",
        "source": label,
        "handle": handle,
        "author": author,
        "url": url,
        "text": text,
        "published": iso(ts) if ts else "",
        "published_exact": bool(ts),
        "images": images,
        "has_video": has_video,
        "video_url": video_url,
        "repost": bool(tw.get("retweeted_status") or tw.get("retweeted_status_result")),
    }


def fetch_x_syndication(handle, label, limit, session):
    """Route 1: the timeline-profile page, which embeds __NEXT_DATA__."""
    url = SYNDICATION_PAGE.format(handle=handle)
    r = session.get(url, timeout=30, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://platform.twitter.com/",
    })
    vlog(f"route 1 -> HTTP {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()

    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ block in response")
    data = json.loads(m.group(1))
    tweets = harvest_tweets(data)
    if not tweets:
        raise RuntimeError("payload had no tweet objects")
    ordered = sorted(tweets.values(),
                     key=lambda t: parse_twitter_date(t.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
    return [normalise_tweet(t, handle, label) for t in ordered[:limit]]


def fetch_x_cdn(handle, label, limit, session):
    """Route 2: the CDN timeline JSON used by older embed widgets."""
    r = session.get(SYNDICATION_CDN, timeout=30, params={
        "screen_name": handle,
        "token": syndication_token(handle),
        "lang": "en",
        "suppress_response_codes": "true",
    }, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://platform.twitter.com/",
    })
    vlog(f"route 2 -> HTTP {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    body = r.json()
    if isinstance(body, dict) and isinstance(body.get("body"), str):
        inner = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', body["body"], re.S)
        body = json.loads(inner.group(1)) if inner else body
    tweets = harvest_tweets(body)
    if not tweets:
        raise RuntimeError("payload had no tweet objects")
    ordered = sorted(tweets.values(),
                     key=lambda t: parse_twitter_date(t.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
    return [normalise_tweet(t, handle, label) for t in ordered[:limit]]


# --------------------------------------------------------------------------
# Facebook, logged out
#
# A public Page renders for logged-out visitors: the post data ships inside
# JSON blobs in <script> tags, behind a login modal that only covers it
# visually. Route 1 reads those blobs with a plain HTTP fetch. Route 2 renders
# the same page in a browser and reads the DOM, which survives payload
# reshuffles better. Neither needs an account.
# --------------------------------------------------------------------------

FB_PAGE_URLS = (
    "https://www.facebook.com/{handle}",
    "https://www.facebook.com/{handle}/posts",
    "https://m.facebook.com/{handle}",
)

MEDIA_KEYS = ("photo_image", "image", "preferred_thumbnail", "full_width_image",
              "large_share_image", "thumbnail_image", "blurred_image", "viewer_image")
VIDEO_KEYS = ("playable_url", "playable_url_quality_hd", "browser_native_hd_url",
              "browser_native_sd_url", "hd_src", "sd_src")


def deep_collect(node, want_keys, limit=24):
    """Pull URLs living under any of want_keys, at any depth."""
    out = []
    for n in walk_json(node):
        if not isinstance(n, dict):
            continue
        for k, v in n.items():
            if k not in want_keys:
                continue
            url = v.get("uri") if isinstance(v, dict) else v
            if isinstance(url, str) and url.startswith("http") and url not in out:
                out.append(url)
                if len(out) >= limit:
                    return out
    return out


NOT_A_POST = {"Comment", "Reply", "Feedback", "CommentsEdge", "User", "Page", "Group"}
URL_FIELDS = ("wwwURL", "url", "permalink_url", "permalink")


def decode_story_id(sid):
    """Facebook's encoded story id embeds the numeric post id.

    'UzpfSTEwMDA2MzY1NjAxNjQzNToxNjg2ODAxNTcwMTE4MzYx' base64-decodes to
    'S:_I100063656016435:1686801570118361' - the trailing number is the post.
    """
    if not isinstance(sid, str) or not sid.startswith("Uzpf"):
        return None
    try:
        raw = base64.b64decode(sid + "==").decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"(\d{6,})\s*$", raw)
    return m.group(1) if m else None


def fragment_key(node):
    """Which post a fragment belongs to, or None if it isn't part of one."""
    pid = node.get("post_id")
    if isinstance(pid, str) and pid.strip():
        return pid.strip()
    return decode_story_id(node.get("id"))


def fragment_payload(node):
    """The useful bits this fragment carries, if any."""
    out = {}

    msg = node.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
        out["text"] = msg["text"].strip()

    ct = node.get("creation_time")
    if isinstance(ct, (int, float)) and ct > 0:
        out["created"] = float(ct)

    for f in URL_FIELDS:
        v = node.get(f)
        if isinstance(v, str) and v.startswith("http") and "facebook.com" in v:
            out["url"] = v
            break

    if "attachments" in node or "comet_sections" in node:
        imgs = [u for u in deep_collect(node, MEDIA_KEYS)
                if isinstance(u, str) and ("scontent" in u or "fbcdn" in u)]
        vids = [u for u in deep_collect(node, VIDEO_KEYS)
                if isinstance(u, str) and u.startswith("http")]
        if imgs:
            out["images"] = imgs
        if vids:
            out["videos"] = vids

    return out


def harvest_fb_posts(node, found=None):
    """Collect Facebook posts, merging the fragments each one is split across.

    A single post arrives as several objects: one carries creation_time and
    attachments, another the message text, another the permalink. They share a
    post_id (or an encoded id that contains it). Treating each fragment as its
    own post yields textless duplicates, so merge by identity instead.
    """
    if found is None:
        found = {}
    for n in walk_json(node):
        if not isinstance(n, dict):
            continue
        if n.get("__typename") in NOT_A_POST or n.get("is_comment"):
            continue
        key = fragment_key(n)
        if not key:
            continue
        part = fragment_payload(n)
        if not part:
            continue
        slot = found.setdefault(key, {})
        for field in ("text", "created", "url"):
            if field in part and field not in slot:
                slot[field] = part[field]
        for field in ("images", "videos"):
            if field in part:
                merged = slot.setdefault(field, [])
                for u in part[field]:
                    if u not in merged:
                        merged.append(u)
    return found


def script_payloads(html):
    """Every JSON object embedded in a <script> tag, best effort.

    Facebook ships its page data as <script type="application/json"> blobs;
    anything that is not parseable JSON is skipped rather than raising.
    """
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        blob = m.group(1).strip()
        if not blob.startswith("{") or len(blob) < 80:
            continue
        try:
            yield json.loads(blob)
        except ValueError:
            continue


def normalise_fb_post(key, slot, handle, label):
    text = (slot.get("text") or "").strip()
    url = slot.get("url") or ""
    if not url and key.isdigit():
        url = f"https://www.facebook.com/{handle}/posts/{key}"

    ts = None
    if slot.get("created"):
        ts = datetime.fromtimestamp(slot["created"], tz=timezone.utc)

    images = (slot.get("images") or [])[:4]
    videos = slot.get("videos") or []

    # Identity must not depend on which URL form Facebook served: the same post
    # appears as /posts/<numeric id> on one run and /posts/pfbid... on the next.
    # `key` is the numeric post id from the fragments, so hash that instead.
    identity = f"https://www.facebook.com/{handle}/posts/{key}"

    return {
        "id": post_id(identity, text or key, "facebook", handle),
        "platform": "facebook",
        "source": label,
        "handle": handle,
        "author": handle,
        "url": url,
        "text": text,
        "published": iso(ts) if ts else "",
        "published_exact": bool(ts),
        "images": images,
        "has_video": bool(videos),
        "video_url": videos[0] if videos else "",
        "repost": False,
    }


def fetch_fb_public_html(handle, label, limit, session):
    """Route 1: logged-out page fetch, post data read out of the script blobs."""
    last = ""
    for template in FB_PAGE_URLS:
        url = handle if handle.startswith("http") else template.format(handle=handle)
        try:
            r = session.get(url, timeout=35, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            })
            vlog(f"public html {url} -> HTTP {r.status_code}, {len(r.content)} bytes")
            if not r.ok:
                last = f"HTTP {r.status_code}"
                continue

            found = {}
            for payload in script_payloads(r.text):
                harvest_fb_posts(payload, found)
            if not found:
                last = "page rendered but held no post objects"
                continue

            posts = [normalise_fb_post(k, v, handle, label) for k, v in found.items()]
            posts = [p for p in posts if p["text"] or p["images"] or p["has_video"]]
            posts.sort(key=lambda p: p.get("published") or "", reverse=True)
            if posts:
                return posts[:limit]
            last = "post objects held no usable content"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
        if handle.startswith("http"):
            break
    raise RuntimeError(last or "no public HTML route succeeded")


# --------------------------------------------------------------------------
# browser routes — logged out by default, logged in when a cookie secret exists
# --------------------------------------------------------------------------

def parse_cookies(raw, default_domain):
    """Accept a Playwright storage_state, a plain cookie array, or a
    'name=value; name2=value2' header string."""
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    cookies = []
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        arr = data.get("cookies", []) if isinstance(data, dict) else data
        for c in arr:
            if not isinstance(c, dict) or "name" not in c:
                continue
            cookies.append({
                "name": c["name"],
                "value": str(c.get("value", "")),
                "domain": c.get("domain") or default_domain,
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            })
    else:
        for pair in raw.split(";"):
            if "=" not in pair:
                continue
            name, _, value = pair.strip().partition("=")
            if name:
                cookies.append({"name": name.strip(), "value": value.strip(),
                                "domain": default_domain, "path": "/", "secure": True})
    return cookies


X_EXTRACT = r"""
(limit) => {
  const out = [];
  for (const a of document.querySelectorAll('article[data-testid="tweet"], article[role="article"]')) {
    if (out.length >= limit) break;
    const timeEl = a.querySelector('time[datetime]');
    const link = timeEl ? timeEl.closest('a[href*="/status/"]') : a.querySelector('a[href*="/status/"]');
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const images = [];
    a.querySelectorAll('[data-testid="tweetPhoto"] img, img[src*="twimg.com/media/"]').forEach(i => {
      let s = i.currentSrc || i.src || ''; if (!s) return;
      s = s.replace(/name=[^&]+/, 'name=large'); if (!images.includes(s)) images.push(s);
    });
    const videos = [];
    a.querySelectorAll('video').forEach(v => {
      if (v.poster && !images.includes(v.poster)) images.push(v.poster);
      const src = v.currentSrc || v.src || '';
      videos.push(src.startsWith('blob:') ? '' : src);
    });
    if (!videos.length && a.querySelector('[data-testid="videoPlayer"], [data-testid="videoComponent"]')) videos.push('');
    const author = (a.querySelector('[data-testid="User-Name"] a[href^="/"]') || {getAttribute:()=>''})
                     .getAttribute('href') || '';
    const text = textEl ? textEl.innerText.trim() : '';
    if (!text && !images.length && !videos.length) continue;
    out.push({
      url: link ? link.href.split('?')[0] : '',
      text: text,
      iso_time: timeEl ? timeEl.getAttribute('datetime') : '',
      raw_time: timeEl ? timeEl.innerText.trim() : '',
      images: images.slice(0, 4),
      videos: videos,
      author: author.replace(/^\//, ''),
      repost: /^\s*\S+\s+reposted/i.test(a.innerText || '')
    });
  }
  return out;
}
"""

FB_EXTRACT = r"""
(limit) => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('div[role="article"]')) {
    if (out.length >= limit) break;
    if (a.parentElement && a.parentElement.closest('div[role="article"]')) continue;

    let url = '', rawTime = '';
    for (const l of a.querySelectorAll('a[href]')) {
      const h = l.getAttribute('href') || '';
      if (/\/(posts|permalink|videos|photos|reel)\//.test(h) || /story_fbid=/.test(h) ||
          /\/share\/(p|v|r)\//.test(h)) {
        const u = new URL(l.href, location.href);
        const keep = ['story_fbid','fbid','id','v'];
        Array.from(u.searchParams.keys()).forEach(k => { if (!keep.includes(k)) u.searchParams.delete(k); });
        u.hash = '';
        url = u.toString().replace(/\?$/, '');
        const label = l.getAttribute('aria-label') || l.innerText || '';
        if (label && label.length < 60) rawTime = label.trim();
        break;
      }
    }

    let text = '';
    const body = a.querySelector('div[data-ad-preview="message"], div[data-ad-comet-preview="message"], [data-testid="post_message"]');
    if (body) text = body.innerText.trim();
    else text = (a.innerText || '').split('\n').filter(l => l.trim().length > 25).slice(0, 6).join('\n').trim();
    text = text.replace(/\s*See more\s*$/i, '').trim();

    const images = [];
    a.querySelectorAll('img').forEach(i => {
      const s = i.currentSrc || i.src || '';
      if (!s || s.startsWith('data:') || !/scontent|fbcdn/.test(s)) return;
      const w = i.naturalWidth || i.width || 0, h = i.naturalHeight || i.height || 0;
      if ((w && w < 200) || (h && h < 200)) return;
      if (!images.includes(s)) images.push(s);
    });
    const videos = [];
    a.querySelectorAll('video').forEach(v => {
      if (v.poster && !images.includes(v.poster)) images.push(v.poster);
      const src = v.currentSrc || v.src || '';
      videos.push(src.startsWith('blob:') ? '' : src);
    });

    const key = url || text.slice(0, 120);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    if (!text && !images.length && !videos.length) continue;
    out.push({ url, text, iso_time: '', raw_time: rawTime,
               images: images.slice(0, 4), videos, author: '', repost: false });
  }
  return out;
}
"""


# A logged-out public Page puts a login modal over the content and locks the
# scroll. The posts are already in the DOM underneath, so clear the overlay
# rather than trying to dismiss it through the UI.
CLEAR_OVERLAY = r"""
() => {
  document.querySelectorAll('div[role="dialog"], [data-nosnippet]').forEach(d => {
    const t = (d.innerText || '').toLowerCase();
    if (t.includes('log in') || t.includes('sign up') || t.includes('create new account')) d.remove();
  });
  // FB pins the body when the modal is up.
  for (const el of [document.body, document.documentElement]) {
    el.style.overflow = 'auto';
    el.style.position = 'static';
    el.style.height = 'auto';
  }
  document.querySelectorAll('div[data-testid="cookie-policy-manage-dialog"]').forEach(d => d.remove());
  return document.querySelectorAll('div[role="article"], article').length;
}
"""


def fetch_with_browser(platform, handle, label, limit, cookies, cfg):
    """Render the page in Chromium and read the DOM.

    Works logged out — pass cookies only when a secret is configured.
    """
    from playwright.sync_api import sync_playwright

    if platform == "x":
        url, script, wait = f"https://x.com/{handle}", X_EXTRACT, "article"
    else:
        url = handle if handle.startswith("http") else f"https://www.facebook.com/{handle}"
        script, wait = FB_EXTRACT, 'div[role="article"]'

    raw = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1360, "height": 1000},
                                  locale="en-US", timezone_id="America/Chicago")
        if cookies:
            ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        try:
            page.keyboard.press("Escape")
            found = page.evaluate(CLEAR_OVERLAY)
            vlog(f"overlay cleared, {found} article node(s) present")
        except Exception as exc:
            vlog(f"overlay clear failed: {exc}")

        current = page.url or ""
        if "/login" in current or "checkpoint" in current or "/i/flow/login" in current:
            ctx.close(); browser.close()
            raise RuntimeError("redirected to a login page" +
                               (" — session cookies expired" if cookies else
                                " — this page is not viewable logged out"))

        page.wait_for_selector(wait, timeout=20000)
        for _ in range(int(cfg.get("scroll_rounds", 3))):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1200)
            try:
                page.evaluate(CLEAR_OVERLAY)   # the modal can re-appear on scroll
            except Exception:
                pass
        raw = page.evaluate(script, limit)
        ctx.close()
        browser.close()

    posts = []
    for item in raw or []:
        ts = parse_twitter_date(item.get("iso_time")) or parse_loose_time(item.get("raw_time"))
        vids = [v for v in (item.get("videos") or [])]
        text = (item.get("text") or "").strip()
        posts.append({
            "id": post_id(item.get("url"), text, platform, handle),
            "platform": platform, "source": label, "handle": handle,
            "author": item.get("author") or handle, "url": item.get("url") or "",
            "text": text, "published": iso(ts) if ts else "",
            "published_exact": bool(item.get("iso_time")),
            "images": item.get("images") or [], "has_video": bool(vids),
            "video_url": next((v for v in vids if v), ""),
            "repost": bool(item.get("repost")),
        })
    return posts


# --------------------------------------------------------------------------
# per-source dispatch
# --------------------------------------------------------------------------

def collect_source(src, cfg, session, secrets):
    platform = src["platform"].lower()
    handle = src["handle"].strip().strip("/")
    label = src.get("label") or handle
    limit = int(cfg.get("max_posts_per_source", 12))
    attempts = []

    if platform == "x":
        routes = [("syndication page", lambda: fetch_x_syndication(handle, label, limit, session)),
                  ("syndication cdn", lambda: fetch_x_cdn(handle, label, limit, session))]
        if secrets.get("x_cookies"):
            routes.append(("logged-in browser",
                           lambda: fetch_with_browser("x", handle, label, limit,
                                                      secrets["x_cookies"], cfg)))
    elif platform == "facebook":
        # Public Pages render for logged-out visitors, so both of these run
        # without an account. Cookies are only a last resort.
        routes = [
            ("public html", lambda: fetch_fb_public_html(handle, label, limit, session)),
            ("public html retry", lambda: (time.sleep(4), fetch_fb_public_html(handle, label, limit, session))[1]),
            ("logged-out browser",
             lambda: fetch_with_browser("facebook", handle, label, limit, [], cfg)),
        ]
        if secrets.get("fb_cookies"):
            routes.append(("logged-in browser",
                           lambda: fetch_with_browser("facebook", handle, label, limit,
                                                      secrets["fb_cookies"], cfg)))
    else:
        return [], {"platform": platform, "handle": handle, "label": label,
                    "ok": False, "route": "", "error": f"unknown platform '{platform}'",
                    "checked": iso(now_utc())}

    for name, fn in routes:
        try:
            posts = fn()
            if posts:
                log(f"  {platform}/{handle}: {len(posts)} post(s) via {name}")
                return posts, {"platform": platform, "handle": handle, "label": label,
                               "ok": True, "route": name, "error": "", "count": len(posts),
                               "checked": iso(now_utc())}
            attempts.append(f"{name}: returned nothing")
        except Exception as exc:
            attempts.append(f"{name}: {type(exc).__name__}: {exc}")
            vlog(f"{name} failed: {exc}")
        time.sleep(1.5)

    log(f"  {platform}/{handle}: FAILED — {'; '.join(attempts)}")
    status = {"platform": platform, "handle": handle, "label": label, "ok": False,
              "route": "", "error": "; ".join(attempts)[:400], "checked": iso(now_utc())}
    # A Facebook page that could not be collected still has one thing left: the
    # platform's own embed widget. Flag it so the page can render that.
    if platform == "facebook":
        status["embed_only"] = True
    return [], status


# --------------------------------------------------------------------------
# preview images — downscaled so the repo does not balloon
# --------------------------------------------------------------------------

def save_preview(url, session, cfg):
    from PIL import Image

    ext = ".jpg"
    name = hashlib.sha1(url.encode()).hexdigest()[:20] + ext
    dest = MEDIA_DIR / name
    if dest.exists() and dest.stat().st_size > 0:
        return f"media/{name}"

    r = session.get(url, timeout=30, headers={"User-Agent": UA, "Referer": "https://x.com/"})
    if not r.ok or len(r.content) > int(cfg.get("max_download_bytes", 12000000)):
        return ""

    img = Image.open(io.BytesIO(r.content))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    max_w = int(cfg.get("preview_max_width", 900))
    if img.width > max_w:
        img = img.resize((max_w, max(1, round(img.height * max_w / img.width))), Image.LANCZOS)
    MEDIA_DIR.mkdir(exist_ok=True)
    img.save(dest, "JPEG", quality=int(cfg.get("preview_quality", 78)), optimize=True)
    return f"media/{name}"


def download_previews(posts, session, cfg):
    if not cfg.get("download_previews", True):
        for p in posts:
            p.setdefault("preview", "")
        return
    for post in posts:
        if post.get("preview"):
            continue
        post["preview"] = ""
        for url in post.get("images", []):
            if not url.startswith("http"):
                continue
            try:
                got = save_preview(url, session, cfg)
                if got:
                    post["preview"] = got
                    break
            except Exception as exc:
                vlog(f"preview failed for {url[:70]}: {exc}")


# --------------------------------------------------------------------------
# merge, prune
# --------------------------------------------------------------------------

def richness(p):
    """How complete a post record is, for picking between duplicates."""
    return (len(p.get("text") or ""), len(p.get("images") or []),
            1 if p.get("url") else 0, 1 if p.get("published_exact") else 0)


def collapse_twins(items):
    """Merge records that are plainly the same post seen twice.

    An account does not publish two different posts in the same second, so
    identical (platform, handle, exact timestamp) means one post that reached us
    under two identities - which happens when Facebook serves a numeric
    permalink one run and a pfbid one the next. Keep the richer record but the
    earlier first_seen, so the post does not jump to the top of the feed.
    """
    best = {}
    passthrough = []
    for p in items:
        if not p.get("published") or not p.get("published_exact"):
            passthrough.append(p)
            continue
        key = (p.get("platform"), (p.get("handle") or "").lower(), p["published"])
        prev = best.get(key)
        if prev is None:
            best[key] = p
            continue
        winner, loser = (p, prev) if richness(p) > richness(prev) else (prev, p)
        seen = [x.get("first_seen") for x in (winner, loser) if x.get("first_seen")]
        if seen:
            winner["first_seen"] = min(seen)
        if not winner.get("preview") and loser.get("preview"):
            winner["preview"] = loser["preview"]
        best[key] = winner
    return list(best.values()) + passthrough


def merge(existing, fresh, cfg):
    by_id = {p["id"]: p for p in existing}
    stamp = iso(now_utc())
    added = 0
    for post in fresh:
        old = by_id.get(post["id"])
        if old:
            post["first_seen"] = old.get("first_seen", stamp)
            post["published"] = post.get("published") or old.get("published", "")
            post["preview"] = post.get("preview") or old.get("preview", "")
            by_id[post["id"]] = post
        else:
            post["first_seen"] = stamp
            by_id[post["id"]] = post
            added += 1

    items = collapse_twins(list(by_id.values()))
    for p in items:
        p["sort_time"] = p.get("published") or p.get("first_seen") or stamp
    items.sort(key=lambda p: p["sort_time"], reverse=True)

    keep_days = int(cfg.get("keep_days", 45))
    if keep_days > 0:
        cutoff = iso(now_utc() - timedelta(days=keep_days))
        items = [p for p in items if p["sort_time"] >= cutoff]
    return items[: int(cfg.get("max_feed_items", 300))], added


def prune_media(items):
    if not MEDIA_DIR.exists():
        return 0
    keep = {Path(p["preview"]).name for p in items if p.get("preview")}
    removed = 0
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.name not in keep:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args):
    global VERBOSE
    VERBOSE = args.verbose

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log(f"{CONFIG_PATH.name} is missing.")
        return 1

    secrets = {
        "x_cookies": parse_cookies(os.environ.get("X_COOKIES", ""), ".x.com"),
        "fb_cookies": parse_cookies(os.environ.get("FB_COOKIES", ""), ".facebook.com"),
    }
    for key, label in (("x_cookies", "X"), ("fb_cookies", "Facebook")):
        if secrets[key]:
            log(f"{label} cookies present ({len(secrets[key])} cookie(s)) — logged-in route enabled")

    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    if args.only:
        sources = [s for s in sources if s.get("platform", "").lower() == args.only]
    if not sources:
        log("No enabled sources.")
        return 1

    session = requests.Session()
    fresh, status = [], []

    for src in sources:
        try:
            posts, st = collect_source(src, cfg, session, secrets)
        except Exception:
            log(traceback.format_exc())
            posts, st = [], {"platform": src.get("platform"), "handle": src.get("handle"),
                             "label": src.get("label"), "ok": False, "route": "",
                             "error": "unhandled error, see run log", "checked": iso(now_utc())}
        fresh.extend(posts)
        status.append(st)

    if fresh:
        download_previews(fresh, session, cfg)

    previous = load_json(FEED_PATH, {})
    existing = previous.get("items", []) if isinstance(previous, dict) else []
    items, added = merge(existing, fresh, cfg)
    removed = prune_media(items)

    ok_count = sum(1 for s in status if s.get("ok"))
    save_json(FEED_PATH, {
        "generated": iso(now_utc()),
        "count": len(items),
        "sources": status,
        "items": items,
    })

    log(f"Done. {len(fresh)} scraped, {added} new, {len(items)} in feed, "
        f"{ok_count}/{len(status)} sources ok"
        f"{f', {removed} stale preview(s) removed' if removed else ''}.")

    # Never fail the workflow just because a platform blocked us — the run
    # still has to commit whatever it did get. Only a total wipeout is an error.
    if ok_count == 0 and not items:
        log("No source succeeded and there is nothing cached — check the errors above.")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="Collect public X / Facebook posts into feed.json")
    ap.add_argument("--only", choices=["x", "facebook"], help="only run one platform")
    ap.add_argument("--verbose", action="store_true", help="log every fetch attempt")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
