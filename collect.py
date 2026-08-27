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
import random
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
                last = f"page rendered but held no post objects ({len(r.content)} bytes)"
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
  const kill = el => { try { el && el.remove(); } catch(e){} };

  // The login CTA is a form, not a dialog, and its wrapper carries no text -
  // so match it structurally and climb to whatever fixed layer contains it.
  const form = document.querySelector('#login_popup_cta_form');
  if (form) {
    let n = form;
    for (let i = 0; i < 10 && n.parentElement && n.parentElement !== document.body; i++) {
      const pos = getComputedStyle(n.parentElement).position;
      n = n.parentElement;
      if (pos === 'fixed' || pos === 'sticky') break;
    }
    kill(n);
  }

  // Modals proper. Do not test their text: the outer node is often empty.
  document.querySelectorAll('[role="dialog"], [aria-modal="true"]').forEach(kill);
  document.querySelectorAll('div[data-testid="cookie-policy-manage-dialog"]').forEach(kill);

  // Any leftover fixed layer covering most of the viewport is a gate, not content.
  document.querySelectorAll('body > div, body > div > div').forEach(d => {
    const s = getComputedStyle(d);
    if (s.position !== 'fixed') return;
    const r = d.getBoundingClientRect();
    if (r.height > window.innerHeight * 0.6 && r.width > window.innerWidth * 0.6) kill(d);
  });

  // Facebook pins the document while the gate is up.
  for (const el of [document.body, document.documentElement]) {
    el.style.setProperty('overflow', 'auto', 'important');
    el.style.setProperty('position', 'static', 'important');
    el.style.setProperty('height', 'auto', 'important');
    el.style.removeProperty('padding-right');
  }
  return document.querySelectorAll('div[role="article"]').length;
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

        # A login *popup* over readable content is not a wall - only an actual
        # redirect to the login page is. Check the path, not the whole URL.
        current = page.url or ""
        path = urlsplit(current).path
        walled = (path.startswith("/login") or path.startswith("/checkpoint")
                  or "/i/flow/login" in current)
        if walled:
            ctx.close(); browser.close()
            raise RuntimeError("redirected to a login page" +
                               (" — session cookies expired" if cookies else
                                " — this page is not viewable logged out"))

        page.wait_for_selector(wait, timeout=20000)

        # Scroll until the post count stops growing. Facebook re-inserts the
        # login gate as you go, so clear it every round or scrolling stops.
        #
        # Two things used to cut this short. The wait after each scroll was
        # 1.4s, which is less than Facebook takes to render the next batch, so
        # a round that was merely slow looked empty; and two such rounds ended
        # the loop. Measured on a live run, the whole scroll finished in four
        # seconds with the count stuck at three - it was quitting before the
        # page had answered. Smaller steps, a longer wait, and more patience
        # before giving up.
        seen_before = 0
        stalled = 0
        rounds = int(cfg.get("scroll_rounds", 10))
        patience = int(cfg.get("scroll_patience", 3))
        step = int(cfg.get("scroll_step_px", 1800))
        pause = int(cfg.get("scroll_pause_ms", 2200))
        for i in range(rounds):
            page.mouse.wheel(0, step)
            page.wait_for_timeout(pause)
            # Lazy-loaded posts arrive on the network, so give a quiet moment
            # a chance to mean "loaded" rather than guessing from the clock.
            try:
                page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            try:
                found = page.evaluate(CLEAR_OVERLAY)
            except Exception:
                found = 0
            if found and found <= seen_before:
                stalled += 1
                if stalled >= patience:
                    vlog(f"scroll stalled at {seen_before} node(s) after {i+1} round(s)")
                    break
            else:
                stalled = 0
            seen_before = max(seen_before, found or 0)
        vlog(f"after scrolling, {seen_before} article node(s) present")

        raw = page.evaluate(script, limit)
        vlog(f"extractor returned {len(raw or [])} post(s) from {seen_before} node(s)")
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

def collect_facebook(handle, label, limit, cfg, session, secrets, budget=None):
    """Union of every route that works, not the first one that does.

    The cheap fetch always runs. The browser pass is rationed: it is slow, and
    - the thing that actually bit us - hammering Facebook with back-to-back
    rendered page loads gets the whole run progressively cut off. `budget`
    carries the remaining browser allowance and a consecutive-wall counter that
    trips a breaker for the rest of the run.
    """
    budget = budget if budget is not None else {"browser": 99, "walls": 0, "eligible": None}
    posts, seen, notes = [], set(), []

    # Once Facebook is serving throttle shells, every further request this run
    # gets one too. Asking anyway does not just waste time - it digs the hole
    # deeper for the next run. Report the remaining sources as skipped and go.
    if budget.get("throttled"):
        note = "throttled earlier this run - not asking again"
        log(f"  facebook/{handle}: skipped ({note})")
        return [], {"platform": "facebook", "handle": handle, "label": label,
                    "ok": False, "route": "", "error": note,
                    "checked": iso(now_utc())}

    def absorb(name, fn):
        try:
            got = fn() or []
        except Exception as exc:
            notes.append(f"{name}: {type(exc).__name__}: {exc}")
            return
        added = 0
        for post in got:
            if post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
            added += 1
        notes.append(f"{name}: +{added}")
        vlog(f"{name} contributed {added} new post(s)")

    absorb("public html", lambda: fetch_fb_public_html(handle, label, limit, session))

    # Throttle detection. Over its budget, Facebook stops refusing and starts
    # answering HTTP 200 with a shell that carries no post data - measured at
    # ~430KB against ~950KB-1MB for a real page, identical to the byte for
    # every page asked for after the cutoff. One page returning nothing is
    # that page's business; three different pages in a row returning nothing
    # is the IP being throttled, and every further request this run digs the
    # hole deeper. Consecutive-across-sources is the signal, not size, so a
    # page that genuinely has no public posts cannot trip it alone.
    if posts:
        budget["starved"] = 0
    elif any("held no post objects" in n for n in notes):
        budget["starved"] = budget.get("starved", 0) + 1
        if budget["starved"] >= 3:
            budget["throttled"] = True
            log("  ! three pages in a row came back stripped - Facebook is "
                "throttling this runner; stopping Facebook for this run")

    eligible = budget.get("eligible")
    # The browser route goes to the same IP, and in the run that exposed this
    # it hit a login wall at the same moment the fetches started coming back
    # stripped. Spending it here would only confirm the throttle.
    may_browse = (budget["browser"] > 0
                  and not budget.get("throttled")
                  and (eligible is None or handle in eligible))

    if len(posts) < limit and may_browse:
        before = len(posts)
        budget["browser"] -= 1
        absorb("logged-out browser",
               lambda: fetch_with_browser("facebook", handle, label, limit, [], cfg))
        if len(posts) == before and "login page" in (notes[-1] if notes else ""):
            budget["walls"] += 1
            if budget["walls"] >= 3:
                budget["browser"] = 0
                log("  ! three login walls in a row - skipping the browser pass "
                    "for the rest of this run")
        else:
            budget["walls"] = 0
    elif len(posts) < limit and not may_browse:
        notes.append("browser: skipped (rationed this run)")

    if len(posts) < limit and secrets.get("fb_cookies") and budget["browser"] > 0:
        absorb("logged-in browser",
               lambda: fetch_with_browser("facebook", handle, label, limit,
                                          secrets["fb_cookies"], cfg))

    posts.sort(key=lambda p: p.get("published") or "", reverse=True)
    posts = posts[:limit]

    if posts:
        log(f"  facebook/{handle}: {len(posts)} post(s) [{'; '.join(notes)}]")
        return posts, {"platform": "facebook", "handle": handle, "label": label,
                       "ok": True, "route": "; ".join(notes), "error": "",
                       "count": len(posts), "checked": iso(now_utc())}

    log(f"  facebook/{handle}: FAILED - {'; '.join(notes)}")
    return [], {"platform": "facebook", "handle": handle, "label": label, "ok": False,
                "route": "", "error": "; ".join(notes)[:400], "embed_only": True,
                "checked": iso(now_utc())}


def collect_source(src, cfg, session, secrets, budget=None):
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
        # Facebook is different from X: the routes are not interchangeable, they
        # see different amounts. A plain fetch returns the one post the page
        # server-renders; a real browser can scroll past the login popup for
        # several more. So run both and union the results rather than stopping
        # at the first route that returns anything.
        return collect_facebook(handle, label, limit, cfg, session, secrets, budget)
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
    return collapse_prefixes(list(best.values()) + passthrough)


def dedup_text(t):
    """Text reduced to what both routes agree on.

    Whitespace and case go, and so does the trailing ellipsis Facebook adds
    when it collapses a long body behind "See more". That ellipsis is the
    whole reason the prefix test used to fail: the truncated copy ends
    "...main structure.…" while the full post continues "...main structure.
    The fire is now...", so the short one is not literally a prefix of the
    long one until the marker is removed.
    """
    s = re.sub(r"\s+", "", (t or "")).lower()
    return re.sub(r"(?:seemore|…|\.{3})+$", "", s)


# Facebook's rendered page collapses long posts behind "See more", so the
# browser route sees a truncated body; the JSON route sees the whole thing.
# Below this length a "prefix" is too weak to prove anything - two posts can
# easily open with the same forty characters.
PREFIX_MIN = 60


def collapse_prefixes(items):
    """Merge a truncated copy of a post into the full one.

    The two Facebook routes disagree about the same post in two ways at once:
    the browser sees "TOMORROW NIGHT'S LUNAR ECLIPSE will ... could make f..."
    with a relative "3h" timestamp, while the page JSON has the full body and
    a real creation time. On top of that Facebook mints a fresh pfbid
    permalink on every view, so neither the URL nor the text nor the timestamp
    matches, and the same post entered the feed again on every run.

    What does hold is that the short version is a prefix of the long one. So:
    same account, one body a prefix of the other, and no more than a day
    apart - one post. Keep the richer record and the earlier first_seen.
    """
    by_account = {}
    for p in items:
        by_account.setdefault((p.get("platform"), (p.get("handle") or "").lower()),
                              []).append(p)

    out = []
    for group in by_account.values():
        # Longest body first, so a full post absorbs its truncated twins
        # rather than several truncations each claiming to be separate.
        group.sort(key=lambda p: len(dedup_text(p.get("text"))), reverse=True)
        kept = []
        for p in group:
            body = dedup_text(p.get("text"))
            match = None
            if len(body) >= PREFIX_MIN:
                for k in kept:
                    kbody = dedup_text(k.get("text"))
                    if kbody.startswith(body) or body.startswith(kbody):
                        if within_a_day(p, k):
                            match = k
                            break
            if match is None:
                kept.append(p)
                continue
            winner, loser = ((p, match) if richness(p) > richness(match)
                             else (match, p))
            seen = [x.get("first_seen") for x in (winner, loser) if x.get("first_seen")]
            if seen:
                winner["first_seen"] = min(seen)
            if not winner.get("preview") and loser.get("preview"):
                winner["preview"] = loser["preview"]
            if not winner.get("images") and loser.get("images"):
                winner["images"] = loser["images"]
            kept[kept.index(match)] = winner
        out.extend(kept)
    return out


def within_a_day(a, b):
    """True when two records are close enough in time to be the same post.

    Relative timestamps ("3h") are parsed at whatever moment the run happened,
    so two sightings of one post can land an hour or more apart. A day is
    generous enough to cover that drift and still refuse to merge a genuine
    repost of the same wording on another day.
    """
    def when(p):
        raw = p.get("published") or ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    ta, tb = when(a), when(b)
    if not ta or not tb:
        return True          # undated records fall back to the text match
    return abs((ta - tb).total_seconds()) <= 86400


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

def rotate_sources(sources, cursor, step):
    """Start each run at a different point in the source list.

    Returns (ordered_sources, cursor_for_next_run). Facebook stops answering
    partway through a run, so whoever is at the back of the list never gets
    collected. Rotating the starting point spreads that loss around instead of
    letting it fall on the same accounts every time.
    """
    if not sources:
        return sources, 0
    try:
        start = int(cursor or 0) % len(sources)
    except (TypeError, ValueError):
        start = 0
    try:
        step = max(1, int(step))
    except (TypeError, ValueError):
        step = 1
    return sources[start:] + sources[:start], (start + step) % len(sources)


def shard_sources(sources, shard, shards):
    """Deal the sources out across parallel jobs, round-robin.

    The throttle is per source IP, and every GitHub-hosted job is a fresh VM.
    Splitting nineteen sources across five jobs means each one asks Facebook
    for about four pages instead of nineteen - under the threshold that was
    costing us most of the list. Round-robin rather than contiguous blocks so
    that when the run order rotates, neighbours do not all move together.
    """
    if shards <= 1:
        return sources
    return [s for i, s in enumerate(sources) if i % shards == shard % shards]


STARVED_MARK = "held no post objects"


def retry_starved(status, cfg, session, cap):
    """Re-ask for the pages that came back stripped, from this job's address.

    GitHub hands out runner IPs from a shared pool and recycles them, so a
    shard can draw an address Facebook is already throttling - its pages come
    back as the ~430KB shell while the other shards do fine. The merge job is
    a sixth fresh VM whose address has not spoken to Facebook this run, so it
    usually gets a straight answer.

    Capped deliberately: if this address is dirty too, working through the
    whole list would only confirm it and burn the address for the next run.
    Anything still missing is picked up by the next beat.

    Returns (posts, handles_recovered). `status` rows are updated in place.
    """
    rows = {id(s): s for s in status}
    targets = [s for s in status
               if not s.get("ok")
               and (s.get("platform") or "").lower() == "facebook"
               and STARVED_MARK in (s.get("error") or "")][:max(0, cap)]
    if not targets:
        return [], []

    log(f"Retrying {len(targets)} starved source(s) from the merge runner: "
        + ", ".join(s.get("handle", "?") for s in targets))

    # No browser on this runner - the merge job does not install Chromium, and
    # the cheap fetch is what the throttle was blocking anyway.
    budget = {"browser": 0, "walls": 0, "eligible": None,
              "throttled": False, "starved": 0}
    posts, recovered = [], []
    for n, row in enumerate(targets):
        src = {"platform": "facebook", "handle": row.get("handle", ""),
               "label": row.get("label") or row.get("handle", "")}
        try:
            got, st = collect_source(src, cfg, session, {}, budget)
        except Exception as exc:
            log(f"  retry of {src['handle']} failed: {type(exc).__name__}: {exc}")
            continue
        if st.get("ok"):
            posts.extend(got)
            recovered.append(src["handle"])
            st["route"] = f"{st.get('route', '')} (recovered on merge runner)".strip()
            rows[id(row)].clear()
            rows[id(row)].update(st)
        if budget.get("throttled"):
            log("  merge runner is throttled too - leaving the rest for the next beat")
            break
        if n < len(targets) - 1:
            time.sleep(random.uniform(3.0, 7.0))

    log(f"Retry recovered {len(recovered)}/{len(targets)} source(s)"
        + (f": {', '.join(recovered)}" if recovered else ""))
    return posts, recovered


def merge_parts(args):
    """Second phase: fold every shard's partial result into feed.json."""
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log(f"{CONFIG_PATH.name} is missing.")
        return 1

    parts = sorted(Path(args.merge).glob("**/*.json"))
    if not parts:
        log(f"No shard results found under {args.merge}.")
        return 1

    fresh, status, cursor = [], [], 0
    for path in parts:
        part = load_json(path, None)
        if not isinstance(part, dict):
            log(f"  ! {path.name} is unreadable - skipping it")
            continue
        fresh.extend(part.get("fresh", []))
        status.extend(part.get("status", []))
        cursor = part.get("next_cursor", cursor) or cursor
        log(f"  {path.name}: {len(part.get('fresh', []))} post(s), "
            f"{sum(1 for s in part.get('status', []) if s.get('ok'))} source(s) ok")

    session = requests.Session()

    # Second chance for anything a dirty shard IP starved, before the feed is
    # written - so a recovered source lands in this run rather than the next.
    if not getattr(args, "no_retry", False):
        recovered_posts, _ = retry_starved(
            status, cfg, session, int(cfg.get("retry_starved_max", 4)))
        fresh.extend(recovered_posts)

    # Previews are downloaded here, once, rather than in every shard: the
    # images come from fbcdn, which has not been the thing refusing us, and
    # doing it in one place keeps the media folder consistent.
    if fresh:
        download_previews(fresh, session, cfg)

    previous = load_json(FEED_PATH, {}) or {}
    existing = previous.get("items", []) if isinstance(previous, dict) else []
    items, added = merge(existing, fresh, cfg)
    removed = prune_media(items)

    status.sort(key=lambda s: (s.get("platform", ""), s.get("handle", "")))
    ok_count = sum(1 for s in status if s.get("ok"))
    save_json(FEED_PATH, {
        "generated": iso(now_utc()),
        "count": len(items),
        "order_cursor": cursor,
        "sources": status,
        "items": items,
    })
    log(f"Merged {len(parts)} shard(s). {len(fresh)} scraped, {added} new, "
        f"{len(items)} in feed, {ok_count}/{len(status)} sources ok"
        f"{f', {removed} stale preview(s) removed' if removed else ''}.")
    if ok_count == 0 and not items:
        log("No source succeeded and there is nothing cached — check the errors above.")
        return 1
    return 0


def run(args):
    global VERBOSE
    VERBOSE = args.verbose

    if getattr(args, "merge", None):
        return merge_parts(args)

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

    previous = load_json(FEED_PATH, {}) or {}

    # Facebook cuts a run off after roughly its first eight page requests -
    # cheap fetches included, not just the browser ones - so a source's
    # POSITION in the run decides whether it is collected at all. Rotating
    # where the list starts each run means nothing is permanently stuck in a
    # losing slot: over a few runs every source gets an early position, and
    # since the feed accumulates, partial runs still converge on full coverage.
    sources, cursor = rotate_sources(sources, previous.get("order_cursor"),
                                     cfg.get("rotate_by", 6))
    log(f"Run order starts at {sources[0].get('platform')}/{sources[0].get('handle')}")

    shards = max(1, int(getattr(args, "shards", 1) or 1))
    if shards > 1:
        sources = shard_sources(sources, int(args.shard or 0), shards)
        log(f"Shard {args.shard}/{shards}: "
            + ", ".join(f"{s.get('platform')}/{s.get('handle')}" for s in sources))
        if not sources:
            log("Nothing in this shard.")
            save_json(Path(args.out), {"fresh": [], "status": [],
                                       "next_cursor": cursor})
            return 0

    # The browser pass is the expensive route, so spend it on the sources at
    # the front of this run's order - the slots least likely to be refused.
    # Each shard gets its own allowance: it is a separate machine with its own
    # standing at Facebook, so one shard's bad luck should not ration another's.
    per_run = max(1, int(cfg.get("browser_pass_per_run", 4)))
    if shards > 1:
        per_run = max(1, int(cfg.get("browser_pass_per_shard", 2)))
    eligible = set(list(dict.fromkeys(
        s["handle"] for s in sources
        if s.get("platform", "").lower() == "facebook"))[:per_run])
    if eligible:
        log(f"Deep pass this run: {', '.join(sorted(eligible))}")

    budget = {"browser": per_run, "walls": 0, "eligible": eligible or None,
              "throttled": False, "starved": 0}

    for n, src in enumerate(sources):
        try:
            posts, st = collect_source(src, cfg, session, secrets, budget)
        except Exception:
            log(traceback.format_exc())
            posts, st = [], {"platform": src.get("platform"), "handle": src.get("handle"),
                             "label": src.get("label"), "ok": False, "route": "",
                             "error": "unhandled error, see run log", "checked": iso(now_utc())}
        fresh.extend(posts)
        status.append(st)
        # Space the requests out. Back-to-back hits are what trip the blocking.
        # No point pacing ourselves between sources we are about to skip.
        if n < len(sources) - 1:
            nxt = sources[n + 1].get("platform", "").lower()
            if not (budget.get("throttled") and nxt == "facebook"):
                time.sleep(random.uniform(3.0, 7.0))

    # A shard's job ends here: hand the raw catch to the merge job, which owns
    # feed.json. Shards must never write it - five jobs committing the same
    # file in parallel is how you lose posts.
    if shards > 1:
        save_json(Path(args.out), {"fresh": fresh, "status": status,
                                   "next_cursor": cursor})
        ok_count = sum(1 for s in status if s.get("ok"))
        log(f"Shard {args.shard} done. {len(fresh)} post(s), "
            f"{ok_count}/{len(status)} sources ok -> {args.out}")
        return 0

    if fresh:
        download_previews(fresh, session, cfg)

    existing = previous.get("items", []) if isinstance(previous, dict) else []
    items, added = merge(existing, fresh, cfg)
    removed = prune_media(items)

    ok_count = sum(1 for s in status if s.get("ok"))
    save_json(FEED_PATH, {
        "generated": iso(now_utc()),
        "count": len(items),
        "order_cursor": cursor,
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
    ap.add_argument("--shard", type=int, default=0, help="which shard this job is")
    ap.add_argument("--shards", type=int, default=1,
                    help="how many parallel jobs are splitting the sources")
    ap.add_argument("--out", default="part.json",
                    help="where a shard writes its partial result")
    ap.add_argument("--merge", help="merge every shard result in this directory "
                                    "into feed.json")
    ap.add_argument("--no-retry", action="store_true",
                    help="with --merge, skip the second attempt at starved sources")
    args = ap.parse_args()
    if args.shards > 1 and not (0 <= args.shard < args.shards):
        ap.error(f"--shard must be between 0 and {args.shards - 1}")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
