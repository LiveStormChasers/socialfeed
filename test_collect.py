"""Offline checks for the GitHub Actions collector.

No network: the syndication routes are exercised against a captured-shape
fixture payload, and the browser route's extraction scripts against fixture DOM.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import collect as C  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- timestamps
print("\n--- timestamps ---")
d = C.parse_twitter_date("Wed Aug 26 18:04:00 +0000 2026")
check("twitter date format parses", d and d.year == 2026 and d.month == 8 and d.hour == 18)
check("ISO with millis parses", C.parse_twitter_date("2026-08-26T18:04:00.000Z") is not None)
check("junk date returns None", C.parse_twitter_date("not a date") is None)
check("empty date returns None", C.parse_twitter_date("") is None)
now = C.now_utc()
check("'5h' relative", abs((now - C.parse_loose_time("5h")).total_seconds() - 18000) < 90)
d = C.parse_loose_time("Yesterday at 4:12 PM")
check("'Yesterday at 4:12 PM'", d and d.hour == 16 and d.minute == 12)
d = C.parse_loose_time("August 20 at 9:03 AM")
check("'August 20 at 9:03 AM'", d and d.month == 8 and d.day == 20)
check("undated future month rolls back a year",
      (C.parse_loose_time("December 30 at 5:00 PM") or now) <= now + timedelta(days=1))

# ------------------------------------------------------------------ post ids
print("\n--- post identity ---")
check("query junk ignored",
      C.post_id("https://x.com/a/status/1?s=20", "t", "x", "a") ==
      C.post_id("https://x.com/a/status/1?ref=z", "t", "x", "a"))
check("different statuses differ",
      C.post_id("https://x.com/a/status/1", "", "x", "a") !=
      C.post_id("https://x.com/a/status/2", "", "x", "a"))
check("fb permalink.php keeps its post id",
      C.post_id("https://www.facebook.com/permalink.php?story_fbid=1&id=9", "", "facebook", "p") !=
      C.post_id("https://www.facebook.com/permalink.php?story_fbid=2&id=9", "", "facebook", "p"))
check("fb tracking params ignored",
      C.post_id("https://www.facebook.com/x/posts/pf1?__cft__=a", "", "facebook", "p") ==
      C.post_id("https://www.facebook.com/x/posts/pf1?__tn__=b", "", "facebook", "p"))
check("trailing slash is not a new post",
      C.post_id("https://www.facebook.com/x/videos/123/", "", "facebook", "p") ==
      C.post_id("https://www.facebook.com/x/videos/123", "", "facebook", "p"))
check("urlless posts fall back to text",
      C.post_id("", "hello there", "x", "a") == C.post_id("", "hello there", "x", "a"))
# One post, many URL shapes: the page JSON says /posts/<id>, the rendered page
# says /reel/<id>. Hashing the shape gave one post two feed entries.
N = "1972707866740878"
shapes = [f"https://www.facebook.com/wxpagecharlie/posts/{N}",
          f"https://www.facebook.com/reel/{N}/",
          f"https://www.facebook.com/wxpagecharlie/videos/{N}",
          f"https://m.facebook.com/reel/{N}",
          f"https://www.facebook.com/permalink.php?story_fbid={N}&id=999",
          f"https://www.facebook.com/photo/?fbid={N}&set=a.1"]
ids = {C.post_id(u, "text", "facebook", "wxpagecharlie") for u in shapes}
check("every Facebook URL shape of one post gives one id", len(ids) == 1, str(ids))
check("a different post number still gives a different id",
      C.post_id(f"https://www.facebook.com/reel/{N[:-1]}9", "t", "facebook", "m") not in ids)
check("the same post under a mislabelled handle still matches",
      C.post_id(f"https://www.facebook.com/reel/{N}", "t", "facebook", "someoneelse") in ids)
# A pfbid permalink is minted fresh on every view, so it identifies a sighting,
# not a post. Two sightings of one post must land on one id; two genuinely
# different posts must not.
IMGA = "https://scontent.fbcdn.net/v/788780260_1590500719093859_2379487695687_n.jpg?stp=x"
check("the same post under two rotating pfbid links is one post",
      C.post_id("https://www.facebook.com/x/posts/pfbid0AAA", "", "facebook", "x", [IMGA]) ==
      C.post_id("https://www.facebook.com/x/posts/pfbid0BBB", "", "facebook", "x",
                [IMGA.replace("?stp=x", "?stp=y&_nc=9")]))
check("two different photos from one account are two posts",
      C.post_id("https://www.facebook.com/x/posts/pfbid0AAA", "", "facebook", "x", [IMGA]) !=
      C.post_id("https://www.facebook.com/x/posts/pfbid0BBB", "", "facebook", "x",
                ["https://scontent.fbcdn.net/v/999111222_1590500719093333_111111111111_n.jpg"]))
check("the same photo from two accounts stays two posts",
      C.post_id("https://www.facebook.com/x/posts/pfbid0AAA", "", "facebook", "x", [IMGA]) !=
      C.post_id("https://www.facebook.com/y/posts/pfbid0AAA", "", "facebook", "y", [IMGA]))
check("with no photo, different wording still separates two posts",
      C.post_id("https://www.facebook.com/x/posts/pfbid0AAA", "rain today", "facebook", "x") !=
      C.post_id("https://www.facebook.com/x/posts/pfbid0BBB", "sun today", "facebook", "x"))
check("a media id is read out of the CDN filename",
      C.facebook_media_id([IMGA]) == "788780260_1590500719093859_2379487695687")
check("a non-CDN image yields no media id", C.facebook_media_id(["https://x/y.jpg"]) == "")
check("short numbers are not mistaken for post ids",
      C.facebook_post_number("https://www.facebook.com/x/posts/123") == "")
check("X identity is untouched by the Facebook rule",
      C.post_id("https://x.com/a/status/5", "t", "x", "a") ==
      C.post_id("https://x.com/a/status/5?s=2", "t", "x", "a"))

check("token derivation is deterministic and non-empty",
      C.syndication_token("NASA") == C.syndication_token("nasa") and len(C.syndication_token("NASA")) > 0)

# --------------------------------------------------- syndication JSON parsing
print("\n--- syndication payload parsing ---")
payload = json.loads((BASE / "tests" / "fixture_syndication.json").read_text())
tweets = C.harvest_tweets(payload)
check("harvester found all 4 tweets regardless of nesting depth", len(tweets) == 4, f"got {len(tweets)}")
check("non-tweet objects not mistaken for tweets", "not-a-tweet" not in tweets)

posts = [C.normalise_tweet(t, "LiveStormChasers", "Live Storm Chasers") for t in tweets.values()]
by_id = {p["url"].rsplit("/", 1)[-1]: p for p in posts}

p = by_id["1811111111111111111"]
check("text extracted", "Barton County" in p["text"])
check("trailing t.co link stripped", "t.co" not in p["text"], p["text"][-30:])
check("permalink built from author + id", p["url"] == "https://x.com/LiveStormChasers/status/1811111111111111111")
check("timestamp exact", p["published"].startswith("2026-08-26T18:04") and p["published_exact"])
check("both photos captured", len(p["images"]) == 2, str(p["images"]))
check("photo upgraded to name=large", all("name=large" in i for i in p["images"] if "name=" in i))
check("photo post not flagged as video", p["has_video"] is False)

v = by_id["1822222222222222222"]
check("video detected via mediaDetails", v["has_video"] is True)
check("highest-bitrate mp4 chosen", v["video_url"].endswith("1280x720.mp4"), v["video_url"])
check("video poster used as preview image", any("video_thumb" in i for i in v["images"]))

alt = by_id["1844444444444444444"]
check("alternate 'video' shape also detected", alt["has_video"] is True)
check("alternate shape mp4 picked up", alt["video_url"].endswith("alt.mp4"), alt["video_url"])
check("alternate shape poster captured", any("alt_poster" in i for i in alt["images"]))

rt = by_id["1833333333333333333"]
check("repost flagged", rt["repost"] is True)
check("repost keeps the original author", rt["author"] == "WxAgencyOne", rt["author"])
check("repost permalink points at original author", "/WxAgencyOne/status/" in rt["url"])

check("all posts got ids", all(len(x["id"]) == 16 for x in posts))
check("ids unique", len({x["id"] for x in posts}) == len(posts))
check("all posts dated", all(x["published"] for x in posts))

# ------------------------------------------------ Facebook, logged out (route 1)
print("\n--- facebook fragment merging ---")
fb_html = (BASE / "tests" / "fixture_fb_public.html").read_text()

check("encoded story id decodes to the post id",
      C.decode_story_id("UzpfSTEwMDA2MzY1NjAxNjQzNToxNjg2ODAxNTcwMTE4MzYx") == "1686801570118361")
check("non-story id decodes to None", C.decode_story_id("Q29tbWVudDoxMjM=") is None)
check("garbage id decodes to None", C.decode_story_id("not-an-id") is None)
check("fragment_key prefers post_id", C.fragment_key({"post_id": "42", "id": "x"}) == "42")
check("fragment_key falls back to the encoded id",
      C.fragment_key({"id": "UzpfSTEwMDA2MzY1NjAxNjQzNToxNjg2ODAxNTcwMTE4MzYx"}) == "1686801570118361")
check("fragment_key returns None for unrelated objects", C.fragment_key({"foo": "bar"}) is None)

blobs = list(C.script_payloads(fb_html))
check("json script blobs parsed", len(blobs) == 2, f"got {len(blobs)}")

found = {}
for b in blobs:
    C.harvest_fb_posts(b, found)
check("five fragments collapse into ONE post", len(found) == 1, f"got {len(found)} keys: {list(found)}")
check("comment did not become a post", "999" not in found)

posts = [C.normalise_fb_post(k, v, "wxpagebravo", "Alpha Forecaster") for k, v in found.items()]
post = posts[0]
check("message text recovered from the body fragment",
      "Palo Pinto" in post["text"], post["text"][:60])
check("creation_time recovered from the metadata fragment",
      post["published"].startswith("2026-") and post["published_exact"], post["published"])
check("permalink recovered", post["url"].endswith("/posts/1686801570118361"), post["url"])
check("photo from one fragment and video thumb from another both kept",
      len(post["images"]) == 2, str(post["images"]))
check("video detected across fragments", post["has_video"] is True)
check("video url captured", post["video_url"].endswith("sd.mp4"), post["video_url"])
check("post has a stable id", len(post["id"]) == 16)

# The old bug: requiring creation_time on every fragment dropped the text.
textless = [p for p in posts if not p["text"]]
check("no textless duplicate posts survive", not textless, str(textless))


class FBSession:
    def __init__(self): self.calls = []
    def get(self, url, **kw):
        self.calls.append(url)
        class R:
            ok = True; status_code = 200; text = fb_html; content = fb_html.encode()
        return R()


fbs = FBSession()
fbposts = C.fetch_fb_public_html("wxpagebravo", "Alpha Forecaster", 12, fbs)
check("logged-out fetch returns the merged post", len(fbposts) == 1, f"got {len(fbposts)}")
check("that post carries its text", "Palo Pinto" in fbposts[0]["text"])
check("first URL tried is the plain page",
      fbs.calls[0] == "https://www.facebook.com/wxpagebravo", fbs.calls[0])
check("stopped after the first working URL", len(fbs.calls) == 1, str(fbs.calls))


class EmptySession:
    def get(self, url, **kw):
        class R:
            ok = True; status_code = 200
            text = "<html><body>nothing here</body></html>"; content = b"x"
        return R()


try:
    C.fetch_fb_public_html("SomePage", "Some Page", 12, EmptySession())
    check("empty page raises rather than returning silence", False)
except RuntimeError as exc:
    check("empty page raises rather than returning silence", "no post objects" in str(exc), str(exc))


class BlockedSession:
    def get(self, url, **kw):
        class R:
            ok = False; status_code = 403; text = ""; content = b""
        return R()


try:
    C.fetch_fb_public_html("SomePage", "Some Page", 12, BlockedSession())
    check("403 from every URL raises", False)
except RuntimeError as exc:
    check("403 from every URL raises", "HTTP 403" in str(exc), str(exc))
check("all three URL templates tried before giving up", len(C.FB_PAGE_URLS) == 3)

# --------------------------------------------------------------- cookie input
print("\n--- cookie secret parsing ---")
ck = C.parse_cookies('{"cookies":[{"name":"auth_token","value":"abc","domain":".x.com","path":"/"}]}', ".x.com")
check("storage_state shape", len(ck) == 1 and ck[0]["name"] == "auth_token")
ck = C.parse_cookies('[{"name":"c_user","value":"1"},{"name":"xs","value":"2"}]', ".facebook.com")
check("plain array shape", len(ck) == 2 and ck[0]["domain"] == ".facebook.com")
ck = C.parse_cookies("c_user=1234; xs=abcd; datr=zz", ".facebook.com")
check("header-string shape", len(ck) == 3 and ck[1]["name"] == "xs", str(ck))
check("empty secret yields nothing", C.parse_cookies("", ".x.com") == [])
check("whitespace-only secret yields nothing", C.parse_cookies("   \n ", ".x.com") == [])
check("malformed JSON degrades quietly", C.parse_cookies("{not json", ".x.com") == [])
check("entries without a name are skipped",
      C.parse_cookies('[{"value":"orphan"},{"name":"ok","value":"1"}]', ".x.com") == [
          {"name": "ok", "value": "1", "domain": ".x.com", "path": "/", "secure": True, "httpOnly": False}])

# ----------------------------------------------------------------- merge/prune
print("\n--- merge and prune ---")
cfg = {"max_feed_items": 300, "keep_days": 45}
items, added = C.merge([], posts, cfg)
check("first merge adds everything", added == len(posts) and len(items) == len(posts))
check("sorted newest first", all(items[i]["sort_time"] >= items[i+1]["sort_time"] for i in range(len(items)-1)))
check("first_seen stamped", all(i.get("first_seen") for i in items))

for i in items:
    i["preview"] = "media/x.jpg" if i["images"] else ""
items2, added2 = C.merge(items, posts, cfg)
check("re-run adds no duplicates", added2 == 0 and len(items2) == len(items))
check("previews survive re-run", all(i["preview"] for i in items2 if i["images"]))
check("first_seen preserved", {i["id"]: i["first_seen"] for i in items} ==
                              {i["id"]: i["first_seen"] for i in items2})

stale = dict(items[0]); stale["id"] = "ancient000000001"
stale["published"] = C.iso(C.now_utc() - timedelta(days=90))
items3, _ = C.merge(items + [stale], [], cfg)
check("posts past keep_days pruned", not any(i["id"] == "ancient000000001" for i in items3))
# Distinct posts, not five copies of one - identical wording from the same
# account is now (correctly) treated as the same post, which would make this
# a dedup test rather than a cap test.
many = [dict(items[0], id=f"cap{n:012d}", text=f"a distinct post number {n}",
             url=f"https://www.facebook.com/x/posts/{700000000000000 + n}",
             published=C.iso(C.now_utc() - timedelta(minutes=n)))
        for n in range(5)]
capped, _ = C.merge([], many, {"max_feed_items": 2, "keep_days": 45})
check("max_feed_items respected", len(capped) == 2, f"got {len(capped)}")

# ------------------------------------------------------- per-source status rows
print("\n--- source status reporting ---")


class DeadSession:
    def get(self, *a, **k):
        raise OSError("network unreachable")


posts_, st = C.collect_source({"platform": "x", "handle": "someone", "label": "Someone"},
                              cfg, DeadSession(), {})
check("failing X source returns no posts", posts_ == [])
check("failure is recorded, not swallowed", st["ok"] is False and "network unreachable" in st["error"])
check("both X routes were attempted", st["error"].count("network unreachable") == 2, st["error"])


# ------------------------------------------------------ facebook route union
print("\n--- facebook route union ---")


class OneSession:
    """Serves the single-post fixture, like the real public-html route does."""
    def get(self, url, **kw):
        class R:
            ok = True; status_code = 200; text = fb_html; content = fb_html.encode()
        return R()


calls = []
real_browser = C.fetch_with_browser


def fake_browser(platform, handle, label, limit, cookies, cfg):
    """Stand in for the browser route, returning posts the fetch cannot see."""
    calls.append((handle, bool(cookies)))
    out = []
    for n in range(4):
        out.append({"id": f"browseronly{n:05d}", "platform": "facebook", "source": label,
                    "handle": handle, "author": handle, "text": f"scrolled post {n}",
                    "url": f"https://www.facebook.com/{handle}/posts/90{n}",
                    "published": f"2026-08-27T0{n}:00:00Z", "published_exact": True,
                    "images": [], "has_video": False, "video_url": "", "repost": False})
    return out


C.fetch_with_browser = fake_browser
try:
    got, st = C.collect_facebook("wxpagebravo", "Alpha Forecaster", 8, cfg, OneSession(), {})
    check("union returns fetch post PLUS browser posts", len(got) == 5, f"got {len(got)}")
    check("the browser route was actually consulted", len(calls) == 1, str(calls))
    check("no cookies were passed on the logged-out attempt", calls and calls[0][1] is False)
    check("status reports both contributions",
          "public html: +1" in st["route"] and "logged-out browser: +4" in st["route"], st["route"])
    check("union source is marked ok", st["ok"] is True and st["count"] == 5)

    calls.clear()
    got2, st2 = C.collect_facebook("wxpagebravo", "Alpha Forecaster", 1, cfg, OneSession(), {})
    check("browser is skipped when the cheap route already met the limit",
          len(calls) == 0 and len(got2) == 1, f"calls={calls} n={len(got2)}")

    calls.clear()
    got3, st3 = C.collect_facebook("Nope", "Nope", 8, cfg, EmptySession(), {})
    check("browser still runs when the cheap route finds nothing", len(calls) == 1)
    check("browser-only results still count as ok", st3["ok"] is True and len(got3) == 4)

    # every route failing must fall back to embed, not pretend success
    def dead_browser(*a, **k):
        raise RuntimeError("redirected to a login page")
    C.fetch_with_browser = dead_browser
    calls.clear()
    got4, st4 = C.collect_facebook("Nope", "Nope", 8, cfg, EmptySession(), {})
    check("total failure falls back to embed", got4 == [] and st4["embed_only"] is True)
    check("failure records every route it tried",
          "public html" in st4["error"] and "logged-out browser" in st4["error"], st4["error"])
finally:
    C.fetch_with_browser = real_browser

check("union dedupes by post id",
      len({p["id"] for p in got}) == len(got))
check("union sorts newest first",
      all(got[i]["published"] >= got[i+1]["published"] for i in range(len(got)-1)))

posts_, st = C.collect_source({"platform": "facebook", "handle": "page", "label": "Page"},
                              cfg, DeadSession(), {})
check("Facebook tries logged-out routes with no cookies present",
      "public html" in st["error"] and "logged-out browser" in st["error"], st["error"])
check("Facebook falls back to embed only after its routes fail", st.get("embed_only") is True)

fbs2 = FBSession()
posts_, st = C.collect_source({"platform": "facebook", "handle": "LiveStormChasers",
                               "label": "Live Storm Chasers"}, cfg, fbs2, {})
check("working Facebook page collects without any cookies",
      st["ok"] is True and len(posts_) == 1, f"ok={st['ok']} n={len(posts_)}")
check("collected Facebook post carries its text", posts_ and "Palo Pinto" in posts_[0]["text"])
check("successful Facebook source reports what each route contributed",
      "public html: +1" in st["route"], st["route"])
check("successful Facebook source is not marked embed_only", "embed_only" not in st)
check("cheapest route wins — no browser was launched", len(fbs2.calls) == 1)

posts_, st = C.collect_source({"platform": "tiktok", "handle": "x", "label": "x"}, cfg, DeadSession(), {})
check("unknown platform reported clearly", "unknown platform" in st["error"])

# ------------------------------------------------------------- image handling
print("\n--- preview optimisation ---")
from PIL import Image  # noqa: E402
import io as _io  # noqa: E402

big = Image.new("RGB", (2400, 1350), (30, 90, 140))
buf = _io.BytesIO(); big.save(buf, "PNG")


class FakeResp:
    ok = True
    content = buf.getvalue()


class FakeSession:
    def get(self, *a, **k):
        return FakeResp()


C.MEDIA_DIR = BASE / "tests" / "tmp_media"
rel = C.save_preview("https://pbs.twimg.com/media/huge.png", FakeSession(), {})
out = BASE / "tests" / "tmp_media" / Path(rel).name
im = Image.open(out)
check("preview downscaled to max width", im.width == 900, f"{im.width}px")
check("aspect ratio preserved", abs(im.height - 506) <= 2, f"{im.height}px")
check("stored as JPEG", im.format == "JPEG")
check("2400px PNG compressed under 120KB", out.stat().st_size < 120000, f"{out.stat().st_size}B")
size_before = out.stat().st_size
rel2 = C.save_preview("https://pbs.twimg.com/media/huge.png", FakeSession(), {})
check("second call reuses the cached file", rel2 == rel and out.stat().st_size == size_before)

feed_items = [{"preview": rel}]
removed = C.prune_media(feed_items)
check("referenced preview not pruned", out.exists() and removed == 0)
removed = C.prune_media([])
check("unreferenced preview pruned", not out.exists() and removed == 1)

# ------------------------------------------------------------- sample output
sample = {"generated": C.iso(C.now_utc()), "count": len(items2),
          "sources": [
              {"platform": "x", "handle": "LiveStormChasers", "label": "Live Storm Chasers",
               "ok": True, "route": "syndication page", "error": "", "count": 4,
               "checked": C.iso(C.now_utc())},
              {"platform": "facebook", "handle": "LiveStormChasers", "label": "Live Storm Chasers",
               "ok": False, "route": "", "error": "no FB_COOKIES secret — using embed fallback",
               "embed_only": True, "checked": C.iso(C.now_utc())}],
          "items": items2}
(BASE / "tests" / "sample_feed.json").write_text(json.dumps(sample, indent=1), encoding="utf-8")
check("sample feed is valid JSON",
      json.loads((BASE / "tests" / "sample_feed.json").read_text()) is not None)

# ------------------------------------------------------- duplicate collapsing
print("\n--- duplicate collapsing ---")
stamp = C.iso(C.now_utc())
numeric = {"id": "aaa", "platform": "facebook", "handle": "wxpagealpha",
           "published": "2026-08-27T00:02:20Z", "published_exact": True,
           "url": "https://www.facebook.com/wxpagealpha/posts/1614491910034140",
           "text": "", "images": ["i"], "preview": "media/old.jpg",
           "first_seen": "2026-08-27T00:36:38Z"}
pfbid = {"id": "bbb", "platform": "facebook", "handle": "wxpagealpha",
         "published": "2026-08-27T00:02:20Z", "published_exact": True,
         "url": "https://www.facebook.com/wxpagealpha/posts/pfbid06qWC8",
         "text": "I'm not going to post something to worry about yet",
         "images": ["i"], "preview": "", "first_seen": "2026-08-27T01:33:54Z"}

out = C.collapse_twins([numeric, pfbid])
check("the two URL forms collapse to one post", len(out) == 1, f"got {len(out)}")
check("the record with text wins", out[0]["text"].startswith("I'm not going"))
check("earlier first_seen is kept so it does not jump the feed",
      out[0]["first_seen"] == "2026-08-27T00:36:38Z", out[0]["first_seen"])
check("preview is rescued from the loser", out[0]["preview"] == "media/old.jpg", out[0]["preview"])

FB = "https://www.facebook.com/x/posts/pfbid0"
other = dict(pfbid, id="ccc", handle="wxpagefoxtrot", url=FB+"OTHERACCOUNT")
check("different accounts at the same second are NOT collapsed",
      len(C.collapse_twins([pfbid, other])) == 2)
later = dict(pfbid, id="ddd", published="2026-08-27T00:02:21Z", url=FB+"LATERPOST",
             text="a completely different post made moments later")
check("a different post seconds later is NOT collapsed",
      len(C.collapse_twins([pfbid, later])) == 2)
# ...but the same wording seconds apart is one post reaching us twice, which is
# what a rotating pfbid permalink looks like.
same_again = dict(pfbid, id="eee", published="2026-08-27T00:02:21Z",
                  url="https://www.facebook.com/x/posts/pfbid0OTHER")
check("the same wording seconds apart IS collapsed",
      len(C.collapse_twins([pfbid, same_again])) == 1)
inexact = dict(pfbid, id="fff", published_exact=False, text="one undated post", url=FB+"UNDATED1")
check("undated posts that differ pass through untouched",
      len(C.collapse_twins([inexact, dict(inexact, id="ggg", url=FB+"UNDATED2",
                                          text="another undated post")])) == 2)
check("empty input is fine", C.collapse_twins([]) == [])

# identity must not depend on which URL form Facebook served
slot = {"text": "hello", "created": 1787790696.0}
a = C.normalise_fb_post("1614491910034140", dict(slot, url="https://www.facebook.com/m/posts/1614491910034140"),
                        "wxpagealpha", "Mike")
b = C.normalise_fb_post("1614491910034140", dict(slot, url="https://www.facebook.com/m/posts/pfbid06qWC8"),
                        "wxpagealpha", "Mike")
check("same post id gives the same feed id regardless of URL form", a["id"] == b["id"])
c = C.normalise_fb_post("9999999999", slot, "wxpagealpha", "Mike")
check("a different post id gives a different feed id", a["id"] != c["id"])

# A reel carries one id in the page JSON and a different one in its permalink,
# so keying off the JSON id gave the same reel two entries in the live feed.
REEL = "https://www.facebook.com/reel/1592783045831172/"
from_json = C.normalise_fb_post("7788990011223344",
                                {"url": REEL, "text": "\U0001F534 How to see the eclipse",
                                 "created": 1787900729.0},
                                "wxpagedelta", "Secrets")
from_browser = C.post_id(REEL, "How to see the eclipse", "facebook", "wxpagedelta")
check("a reel gets one id whichever route found it",
      from_json["id"] == from_browser, f'{from_json["id"]} vs {from_browser}')
check("a different reel is still a different post",
      C.post_id("https://www.facebook.com/reel/1592783045831999/", "t",
                "facebook", "wxpagedelta") != from_browser)
# posts whose permalink carries no number must still key off the fragment id,
# or a rotating pfbid would give one post a new identity every run
pf1 = C.normalise_fb_post("1686801570118361",
                          {"url": "https://www.facebook.com/x/posts/pfbid0AAA",
                           "text": "hello", "created": 1787900729.0}, "x", "X")
pf2 = C.normalise_fb_post("1686801570118361",
                          {"url": "https://www.facebook.com/x/posts/pfbid0ZZZ",
                           "text": "hello", "created": 1787900729.0}, "x", "X")
check("a rotating pfbid link still resolves to one post", pf1["id"] == pf2["id"])

check("a leading marker emoji does not defeat text matching",
      C.dedup_text("\U0001F534 How to see the eclipse") == C.dedup_text("How to see the eclipse"))
check("an emoji anywhere is ignored, leading or inline",
      C.dedup_text("storm \U0001F534 warning") == C.dedup_text("storm warning"))
check("but different words still separate two posts",
      C.dedup_text("storm warning") != C.dedup_text("storm watch"))
check("the displayed url still uses what Facebook served",
      b["url"].endswith("pfbid06qWC8"), b["url"])


# ---------------------------------------------- truncated-twin collapsing
print("\n--- truncated twins ---")

FULL = ("TOMORROW NIGHT'S LUNAR ECLIPSE will feature ideal viewing conditions "
        "across much of the Midwest and Central Plains under clear skies! "
        "However, cloud cover could make for poor viewing across the coastal "
        "Pacific Northwest, parts of California and Nevada, the northern "
        "Rockies, and a large swatch stretching from the Delta Coast up "
        "through the Northeast.")
CUT = ("TOMORROW NIGHT'S LUNAR ECLIPSE will feature ideal viewing conditions "
       "across much of the Midwest and Central Plains under clear skies! "
       "However, cloud cover could make f\u2026")      # the real trailing ellipsis


def fbpost(pid, text, when, exact, url, imgs=None):
    return {"id": pid, "platform": "facebook", "handle": "wxpagecharlie",
            "label": "Charlie Chaser", "author": "Charlie Chaser", "text": text,
            "url": url, "images": imgs or [], "has_video": False, "video_url": "",
            "repost": False, "published": when, "published_exact": exact,
            "first_seen": when, "preview": ""}


# the exact pair from the feed: browser-truncated + relative time vs full + exact
browser = fbpost("aaa", CUT, "2026-08-27T01:10:00Z", False,
                 "https://www.facebook.com/wxpagecharlie/posts/pfbid02AAA")
page = fbpost("bbb", FULL, "2026-08-27T00:52:31Z", True,
              "https://www.facebook.com/wxpagecharlie/posts/pfbid02BBB")
out = C.collapse_twins([browser, page])
check("a truncated copy collapses into the full post", len(out) == 1, str(len(out)))
check("the full text is the one kept", out and out[0]["text"] == FULL)
check("the exact timestamp wins", out and out[0]["published_exact"] is True)
check("first_seen keeps the earlier sighting so it does not jump the feed",
      out and out[0]["first_seen"] == "2026-08-27T00:52:31Z", out[0]["first_seen"])

# the WxNetworkOne case: several truncated copies, all different pfbids
copies = [fbpost(f"c{i}", CUT, f"2026-08-27T0{i}:00:00Z", False,
                 f"https://www.facebook.com/wxpagecharlie/posts/pfbid0{i}")
          for i in range(1, 5)]
check("four pfbid copies of one post collapse to one",
      len(C.collapse_twins(copies)) == 1, str(len(C.collapse_twins(copies))))
check("and they still collapse when the full version arrives later",
      len(C.collapse_twins(copies + [page])) == 1)

# things that must NOT be merged
other = fbpost("zzz", "Completely different post about a tornado warning in "
                      "Oklahoma this evening, please take shelter now.",
               "2026-08-27T01:00:00Z", True,
               "https://www.facebook.com/wxpagecharlie/posts/pfbid0ZZZ")
check("a genuinely different post is left alone",
      len(C.collapse_twins([page, other])) == 2)

elsewhere = dict(page, id="ddd", handle="WxNetworkOne", label="WxNetworkOne",
                 url="https://www.facebook.com/WxNetworkOne/posts/pfbid0THEIRS")
check("the same wording from a different account is not merged",
      len(C.collapse_twins([page, elsewhere])) == 2)

old_repost = fbpost("eee", FULL, "2026-08-20T00:52:31Z", True,
                    "https://www.facebook.com/wxpagecharlie/posts/pfbid0EEE")
check("the same wording a week later is a repost, not a duplicate",
      len(C.collapse_twins([page, old_repost])) == 2)

short_a = fbpost("f1", "Radar update", "2026-08-27T01:00:00Z", True, "u1")
short_b = fbpost("f2", "Radar update tonight", "2026-08-27T01:30:00Z", True, "u2")
check("a short body is not merged into a longer one that merely starts the same",
      len(C.collapse_twins([short_a, short_b])) == 2)

# the live bug: a very short post, identical wording, new pfbid each run
dolly_a = fbpost("d1", "DOLLY, is that you?", "2026-08-27T02:00:00Z", False,
                 "https://www.facebook.com/wxpagecharlie/posts/pfbid0D1")
dolly_b = fbpost("d2", "DOLLY, is that you?", "2026-08-27T03:00:00Z", False,
                 "https://www.facebook.com/wxpagecharlie/posts/pfbid0D2")
check("identical short posts collapse even below the prefix floor",
      len(C.collapse_twins([dolly_a, dolly_b])) == 1,
      str(len(C.collapse_twins([dolly_a, dolly_b]))))
dolly_c = fbpost("d3", "DOLLY, is that you?", "2026-08-20T03:00:00Z", False, "u9")
check("but the same short post a week later is still a separate post",
      len(C.collapse_twins([dolly_a, dolly_c])) == 2)
check("two different short posts are still left alone",
      len(C.collapse_twins([
          fbpost("s1", "Radar update", "2026-08-27T01:00:00Z", True, "u1"),
          fbpost("s2", "Warning issued", "2026-08-27T01:30:00Z", True, "u2")])) == 2)

# a truncated record must be able to donate what the full one lacks
rich_cut = fbpost("g1", CUT, "2026-08-27T01:10:00Z", False, "u3",
                  imgs=["https://cdn/a.jpg"])
bare_full = fbpost("g2", FULL, "2026-08-27T00:52:31Z", True, "u4")
merged = C.collapse_twins([rich_cut, bare_full])
check("the surviving record inherits images the winner was missing",
      len(merged) == 1 and merged[0]["images"] == ["https://cdn/a.jpg"],
      str(merged[0].get("images")))

# The live pair that survived a 60-character floor: 48 shared characters,
# truncation marked with a mix of ellipsis and dots.
atl_short = "We may have a new tropical depression in the Atlantic.\u2026....."
atl_full = ("We may have a new tropical depression in the Atlantic...\n\nInvest 96L has "
            "become better organized over the last few hours, and recent evidence "
            "suggests a closed centre of circulation.")
pair = C.collapse_twins([
    fbpost("t1", atl_short, "2026-08-27T05:48:15Z", False, "u10"),
    fbpost("t2", atl_full, "2026-08-27T05:47:37Z", True, "u11")])
check("a 48-character shared opening now collapses", len(pair) == 1, str(len(pair)))
check("and the full text is what survives", pair and pair[0]["text"] == atl_full)
check("mixed ellipsis and dots normalise the same",
      C.dedup_text("the Atlantic.\u2026.....") == C.dedup_text("the Atlantic..."))

# the floor still has to refuse posts that merely share an opening
check("two different warnings sharing 40+ characters are NOT merged",
      len(C.collapse_twins([
          fbpost("w1", "SEVERE THUNDERSTORM WARNING for Dallas County until 9pm",
                 "2026-08-27T05:00:00Z", True, "u12"),
          fbpost("w2", "SEVERE THUNDERSTORM WARNING for Tarrant County until 9pm",
                 "2026-08-27T05:10:00Z", True, "u13")])) == 2)

# The Foxtrot Forecaster pair: one route dropped an inline emoji the other kept, and the
# shared opening was only 34 letters - under the length floor. The truncation
# marker on the short copy is what makes it conclusive.
gd_short = "What a sunset!  Pinellas County at its best. \u2026"
gd_full = ("What a sunset! \U0001F305 Pinellas County at its best.\n\n"
           "\U0001F4F7: Cynthia Meyer McBee\n\U0001F4CD: Belleair, Florida")
gd = C.collapse_twins([fbpost("gd1", gd_short, "2026-08-27T13:24:00Z", False, "u20"),
                       fbpost("gd2", gd_full, "2026-08-27T13:23:00Z", True, "u21")])
check("a visibly truncated short body collapses into the full one",
      len(gd) == 1, str(len(gd)))
check("and the full text survives", gd and "Belleair" in gd[0]["text"])

check("a short body with NO truncation mark does not swallow a longer post",
      len(C.collapse_twins([
          fbpost("n1", "Rain today", "2026-08-27T13:00:00Z", True, "u22"),
          fbpost("n2", "Rain today across the whole bay area this afternoon",
                 "2026-08-27T13:05:00Z", True, "u23")])) == 2)
check("a truncated four-letter stub is still too little to match on",
      len(C.collapse_twins([
          fbpost("n3", "Rain \u2026", "2026-08-27T13:00:00Z", True, "u24"),
          fbpost("n4", "Rain today across the whole bay area this afternoon",
                 "2026-08-27T13:05:00Z", True, "u25")])) == 2)
check("truncation marks are recognised in every form Facebook uses",
      all(C.looks_truncated(x) for x in ("abc \u2026", "abc...", "abc See more", "abc see more"))
      and not C.looks_truncated("abc."))

# the exact pass: same post number wins regardless of stored id or text
same_number = C.collapse_twins([
    dict(fbpost("old", "short", "2026-08-27T13:06:06Z", False,
                "https://www.facebook.com/x/reel/1592783045831172/")),
    dict(fbpost("new", "a much longer version of the very same post",
                "2026-08-27T13:05:29Z", True,
                "https://www.facebook.com/x/reel/1592783045831172"))])
check("records sharing a post number collapse whatever id they were stored under",
      len(same_number) == 1, str(len(same_number)))
check("and the richer record wins", same_number and same_number[0]["text"].startswith("a much"))

# The live Echo Meteorologist pair: one route scraped the byline as body text, the
# other had no text at all, and the permalink was a pfbid. Only the photo was
# common to both.
MC_URL = "https://www.facebook.com/wxpageecho/posts/pfbid02jT6PHpLW"
MC_IMG = "https://scontent.fbcdn.net/v/788780260_1590500719093859_2379487695687_n.jpg?stp=x"
def mcpost(pid, text, img, ts, exact, preview="", url=None):
    return dict(fbpost(pid, text, ts, exact, url or MC_URL),
                handle="wxpageecho", images=[img], preview=preview)
mc = C.collapse_twins([
    mcpost("mc1", "Chief Meteorologist Echo Meteorologist\nJust now", MC_IMG,
           "2026-08-27T16:27:39Z", False, "media/aa.jpg"),
    mcpost("mc2", "", MC_IMG.replace("?stp=x", "?stp=y"),
           "2026-08-27T16:27:11Z", True)])
check("a post with junk text on one side and none on the other collapses on its photo",
      len(mc) == 1, str(len(mc)))
check("different photos from one account are not collapsed",
      len(C.collapse_twins([
          mcpost("mc3", "one", MC_IMG, "2026-08-27T16:00:00Z", True),
          mcpost("mc4", "two",
                 "https://scontent.fbcdn.net/v/999111222_1590500719093333_111111111111_n.jpg",
                 "2026-08-27T16:05:00Z", True, "", MC_URL + "OTHER")])) == 2)
check("the same photo from two accounts is not collapsed",
      len(C.collapse_twins([
          mcpost("mc5", "x", MC_IMG, "2026-08-27T16:00:00Z", True),
          dict(mcpost("mc6", "x", MC_IMG, "2026-08-27T16:05:00Z", True,
                      "", "https://www.facebook.com/WxNetworkTwo/posts/pfbid0FOX"),
               handle="WxNetworkTwo")])) == 2)

# The live Charlie Chaser case: a short post whose body came back with a
# DIFFERENT commenter scraped into it on every run, so one post became four.
# The identical permalink was the only thing all four shared.
MV = "https://www.facebook.com/wxpagecharlie/posts/pfbid0g7uKSzPZ3LaUnNYJgkPup4"
def mv(pid, tail, seen):
    return dict(fbpost(pid, "The weather is quiet\u2026\nAll reactions:\n" + tail,
                       "2026-08-27T17:00:00Z", False, MV),
                handle="wxpagecharlie", first_seen=seen)
four = C.collapse_twins([
    mv("q1", "Nathaniel Parsley\nSo what I'm hearing is", "2026-08-27T17:01:00Z"),
    mv("q2", "Sandra Dee\nWe can talk about clouds", "2026-08-27T17:11:00Z"),
    mv("q3", "Lynne Christie\nUh oh, the 'Q' word", "2026-08-27T17:21:00Z"),
    mv("q4", "Emily Frink\nShhhh you are not supposed to", "2026-08-27T17:31:00Z")])
check("four sightings of one post sharing a permalink collapse to one",
      len(four) == 1, str(len(four)))
check("and the earliest first_seen is kept",
      four and four[0]["first_seen"] == "2026-08-27T17:01:00Z")
check("two different permalinks are still two posts",
      len(C.collapse_twins([
          dict(fbpost("u1", "one", "2026-08-27T17:00:00Z", True, MV + "AAA"), handle="m"),
          dict(fbpost("u2", "two", "2026-08-27T17:05:00Z", True, MV + "BBB"), handle="m")])) == 2)
check("records with no permalink are not swept together",
      len(C.collapse_twins([
          dict(fbpost("n1", "alpha post text", "2026-08-27T17:00:00Z", True, ""), handle="m"),
          dict(fbpost("n2", "beta post text", "2026-08-27T17:05:00Z", True, ""), handle="m")])) == 2)

# Comment furniture already stored in the feed has to be cleaned on the way
# through, because the polluted copy is LONGER and would win "keep the richer
# record" against a clean one.
check("a body is cut where its comment thread starts",
      C.clean_body("The weather is quiet\u2026\nAll reactions:\nView more comments\nBob\nnice")
      == "The weather is quiet\u2026")
check("a body with no furniture is untouched",
      C.clean_body("A clean post with no furniture") == "A clean post with no furniture")
check("empty text is safe", C.clean_body("") == "" and C.clean_body(None) == "")
check("a mid-sentence mention of reactions is NOT treated as furniture",
      C.clean_body("People had all reactions to this and we saw more comments than usual")
      == "People had all reactions to this and we saw more comments than usual")
check("multi-line bodies keep every line above the thread",
      C.clean_body("Line one\nLine two\nAll reactions:\nBob") == "Line one\nLine two")

def mrec(pid, text, when):
    return dict(fbpost(pid, text, when, True,
                       "https://www.facebook.com/m/posts/pfbid0" + pid), handle="m")
merged, _ = C.merge([mrec("old", "Quiet day\nAll reactions:\nBob\nnice", "2026-08-28T21:00:00Z")],
                    [mrec("new", "Fresh post here", "2026-08-28T22:00:00Z")],
                    {"max_feed_items": 100, "keep_days": 30})
check("merge cleans records already stored in the feed",
      [i["text"] for i in merged if i["id"] == "old"] == ["Quiet day"])
check("merge leaves a clean fresh record alone",
      any(i["text"] == "Fresh post here" for i in merged))

check("collapsing an empty list is safe", C.collapse_twins([]) == [])
check("posts with no text at all are not collapsed together",
      len(C.collapse_twins([fbpost("h1", "", "2026-08-27T01:00:00Z", True, "u5"),
                            fbpost("h2", "", "2026-08-27T02:00:00Z", True, "u6")])) == 2)


# the ellipsis Facebook appends is what broke the prefix test in the live feed
check("a trailing ellipsis does not defeat the prefix match",
      C.dedup_text("hello world\u2026") == C.dedup_text("hello world"))
check("a three-dot ellipsis is stripped too",
      C.dedup_text("hello world...") == C.dedup_text("hello world"))
check("a 'See more' affordance is stripped",
      C.dedup_text("hello world… See more") == C.dedup_text("hello world"))
check("punctuation is ignored for matching, words are not",
      C.dedup_text("a.b.c") == "abc" and C.dedup_text("a.b.d") != C.dedup_text("a.b.c"))


# ---------------------------------------------- sharding across runners
print("\n--- sharding ---")

nineteen = [{"handle": f"h{i}"} for i in range(19)]

check("one shard is the whole list untouched",
      C.shard_sources(nineteen, 0, 1) == nineteen)

groups = [C.shard_sources(nineteen, k, 5) for k in range(5)]
flat = [s["handle"] for g in groups for s in g]
check("every source lands in exactly one shard",
      sorted(flat) == sorted(s["handle"] for s in nineteen) and len(flat) == 19,
      f"{len(flat)} placed")
check("no source is dealt to two shards", len(set(flat)) == 19)
check("shards are evenly sized", sorted(len(g) for g in groups) == [3, 4, 4, 4, 4],
      str([len(g) for g in groups]))
check("a shard asks Facebook for far fewer pages than the throttle allows",
      max(len(g) for g in groups) <= 4, str(max(len(g) for g in groups)))

# rotation must actually reshuffle who shares a shard, or a bad IP always
# punishes the same four accounts
rot, _ = C.rotate_sources(nineteen, 6, 6)
after = [s["handle"] for s in C.shard_sources(rot, 0, 5)]
check("rotation changes a shard's membership between runs",
      after != [s["handle"] for s in groups[0]], str(after))

check("an out-of-range shard index wraps rather than returning nothing",
      len(C.shard_sources(nineteen, 7, 5)) > 0)
check("more shards than sources is safe",
      sum(len(C.shard_sources(nineteen, k, 40)) for k in range(40)) == 19)


# ---------------------------------------------- merging shard results
print("\n--- merging shard results ---")

import tempfile, types  # noqa: E402

tmp = Path(tempfile.mkdtemp())
(parts := tmp / "parts").mkdir()


def a_post(pid, handle):
    # Dated relative to now, not pinned. These fixtures used to carry a fixed
    # date, which quietly started failing the merge checks once that date aged
    # past keep_days: the posts were pruned and every count came back 0. The
    # merge behaviour is what is under test here, not retention.
    recent = C.iso(C.now_utc())
    return {"id": pid, "platform": "facebook", "handle": handle, "label": handle,
            "author": handle, "text": f"post {pid}", "url": f"https://x/{pid}",
            "images": [], "has_video": False, "video_url": "", "repost": False,
            "published": recent, "published_exact": True,
            "first_seen": recent}


C.save_json(parts / "part-0.json", {
    "fresh": [a_post("aaa", "one")],
    "status": [{"platform": "facebook", "handle": "one", "ok": True}],
    "next_cursor": 11})
C.save_json(parts / "part-1.json", {
    "fresh": [a_post("bbb", "two")],
    "status": [{"platform": "facebook", "handle": "two", "ok": False}],
    "next_cursor": 11})
(parts / "part-broken.json").write_text("{ this is not json")

_feed, _cfgp = C.FEED_PATH, C.CONFIG_PATH
_prev = C.download_previews
try:
    C.FEED_PATH = tmp / "feed.json"
    C.CONFIG_PATH = _cfgp
    C.download_previews = lambda *a, **k: None      # no network in tests
    rc = C.merge_parts(types.SimpleNamespace(merge=str(parts)))
    out = json.loads((tmp / "feed.json").read_text())
    check("merge succeeds even with one corrupt shard file", rc == 0)
    check("posts from every shard survive the merge", out["count"] == 2, str(out["count"]))
    check("status rows from every shard survive", len(out["sources"]) == 2)
    check("the cursor is carried through to the next run", out["order_cursor"] == 11)
    check("a corrupt shard does not wipe the good ones",
          {i["id"] for i in out["items"]} == {"aaa", "bbb"})

    # merging must never drop what is already in the feed
    rc = C.merge_parts(types.SimpleNamespace(merge=str(parts)))
    out2 = json.loads((tmp / "feed.json").read_text())
    check("re-merging the same shards does not duplicate posts",
          out2["count"] == 2, str(out2["count"]))

    empty = tmp / "empty"
    empty.mkdir()
    check("a merge with no shard files reports failure rather than writing an empty feed",
          C.merge_parts(types.SimpleNamespace(merge=str(empty))) == 1)
    check("and the existing feed is left alone",
          json.loads((tmp / "feed.json").read_text())["count"] == 2)
finally:
    C.FEED_PATH, C.CONFIG_PATH, C.download_previews = _feed, _cfgp, _prev


# ---------------------------------------------- retrying starved sources
print("\n--- retry of starved sources ---")

STARVED_ERR = "public html: RuntimeError: page rendered but held no post objects (439957 bytes)"


def rows():
    return [
        {"platform": "facebook", "handle": "good", "label": "Good", "ok": True,
         "route": "public html: +1"},
        {"platform": "facebook", "handle": "starved1", "label": "S1", "ok": False,
         "error": STARVED_ERR},
        {"platform": "facebook", "handle": "starved2", "label": "S2", "ok": False,
         "error": STARVED_ERR},
        {"platform": "facebook", "handle": "reallybroken", "label": "B", "ok": False,
         "error": "public html: HTTP 404"},
        {"platform": "x", "handle": "xthing", "label": "X", "ok": False,
         "error": "held no post objects"},
    ]


_real_cs = C.collect_source
try:
    asked = []

    def fake_collect(src, cfg_, session_, secrets_, budget_=None):
        asked.append(src["handle"])
        post = a_post("r-" + src["handle"], src["handle"])
        return [post], {"platform": "facebook", "handle": src["handle"],
                        "label": src["label"], "ok": True, "route": "public html: +1"}

    C.collect_source = fake_collect
    st = rows()
    got, rec = C.retry_starved(st, {}, None, 4)
    check("only starved sources are retried", sorted(asked) == ["starved1", "starved2"],
          str(asked))
    check("a genuinely broken source is not retried", "reallybroken" not in asked)
    check("non-Facebook sources are not retried", "xthing" not in asked)
    check("recovered posts come back", len(got) == 2, str(len(got)))
    # Recovery is reported by tag, not by handle: these names would otherwise
    # reach the public run log.
    check("recovered sources are reported, by tag",
          sorted(rec) == sorted([C.src_tag("facebook", "starved1"),
                                 C.src_tag("facebook", "starved2")]), str(rec))
    check("the status row is updated in place to ok",
          [r for r in st if r["handle"] == "starved1"][0]["ok"] is True)
    check("the row says it was recovered on the merge runner",
          "recovered" in [r for r in st if r["handle"] == "starved1"][0]["route"])
    check("the already-good row is untouched",
          [r for r in st if r["handle"] == "good"][0]["route"] == "public html: +1")

    asked.clear()
    C.retry_starved(rows(), {}, None, 1)
    check("the cap limits how many are retried", len(asked) == 1, str(asked))

    asked.clear()
    C.retry_starved(rows(), {}, None, 0)
    check("a cap of zero retries nothing", asked == [])
    check("nothing to retry is not an error", C.retry_starved([], {}, None, 4) == ([], []))

    # a retry that fails must leave the original failure visible, not hide it
    def failing(src, cfg_, session_, secrets_, budget_=None):
        asked.append(src["handle"])
        return [], {"platform": "facebook", "handle": src["handle"],
                    "label": src["label"], "ok": False, "error": STARVED_ERR}

    C.collect_source = failing
    st = rows()
    got, rec = C.retry_starved(st, {}, None, 4)
    check("a failed retry recovers nothing", (got, rec) == ([], []))
    check("and the source still reads as failed",
          [r for r in st if r["handle"] == "starved1"][0]["ok"] is False)

    # if the merge runner is throttled too, stop rather than burn the address
    def throttling(src, cfg_, session_, secrets_, budget_=None):
        asked.append(src["handle"])
        budget_["throttled"] = True
        return [], {"platform": "facebook", "handle": src["handle"],
                    "label": src["label"], "ok": False, "error": STARVED_ERR}

    C.collect_source = throttling
    asked.clear()
    C.retry_starved(rows(), {}, None, 4)
    check("a throttled merge runner stops after the first attempt",
          len(asked) == 1, str(asked))

    # an exception in one retry must not abandon the rest
    retry_calls = {"n": 0}

    def explodes_once(src, cfg_, session_, secrets_, budget_=None):
        retry_calls["n"] += 1
        if retry_calls["n"] == 1:
            raise RuntimeError("boom")
        return fake_collect(src, cfg_, session_, secrets_, budget_)

    C.collect_source = explodes_once
    asked.clear()
    got, rec = C.retry_starved(rows(), {}, None, 4)
    check("one exploding retry does not stop the next",
          rec == [C.src_tag("facebook", "starved2")], str(rec))
finally:
    C.collect_source = _real_cs


# ---------------------------------------------- throttle-shell breaker
print("\n--- throttle shell ---")


class ShellSession:
    """Facebook's throttle response: HTTP 200, no post data in the page."""
    def __init__(self):
        self.hits = []

    def get(self, url, **kw):
        self.hits.append(url)
        class R:
            ok = True
            status_code = 200
            text = "<html><body>nothing useful here</body></html>"
            content = b"x" * 440000
        return R()


try:
    C.fetch_fb_public_html("someone", "Someone", 8, ShellSession())
    check("a page with no post objects is reported as a failure", False)
except RuntimeError as exc:
    check("a page with no post objects is reported as a failure",
          "held no post objects" in str(exc), str(exc))
    check("the failure records the response size, which is what exposed this",
          "440000 bytes" in str(exc), str(exc))

B = lambda: {"browser": 0, "walls": 0, "eligible": None,
             "throttled": False, "starved": 0}

# one starved page is that page's problem, not a throttle
b = B()
C.collect_facebook("one", "one", 8, cfg, ShellSession(), {}, b)
check("one stripped page does not trip the breaker", b["throttled"] is False)
check("but it is counted", b["starved"] == 1)
C.collect_facebook("two", "two", 8, cfg, ShellSession(), {}, b)
check("two stripped pages still do not trip it", b["throttled"] is False)
_, st = C.collect_facebook("three", "three", 8, cfg, ShellSession(), {}, b)
check("three in a row trips the breaker", b["throttled"] is True)

after = ShellSession()
_, st2 = C.collect_facebook("later", "later", 8, cfg, after, {}, b)
check("later sources make no request at all once throttled",
      after.hits == [], str(after.hits))
check("skipped sources say why", "throttled" in st2["error"], st2["error"])

# a page that works must clear the count, so scattered empties never add up
b2 = B()
C.collect_facebook("x1", "x1", 8, cfg, ShellSession(), {}, b2)
C.collect_facebook("x2", "x2", 8, cfg, ShellSession(), {}, b2)
C.collect_facebook("ok", "ok", 8, cfg, OneSession(), {}, b2)
check("a success resets the starved count", b2["starved"] == 0)
C.collect_facebook("x3", "x3", 8, cfg, ShellSession(), {}, b2)
check("so two empties either side of a success do not trip it",
      b2["throttled"] is False)

# the browser route must not be spent confirming a throttle
b3 = B(); b3["browser"] = 4; b3["throttled"] = True
C.collect_facebook("nope", "nope", 8, cfg, ShellSession(), {}, b3)
check("a throttled run does not spend the browser budget", b3["browser"] == 4)


# ---------------------------------------------- run-order rotation
print("\n--- run order rotation ---")

srcs = [{"handle": h} for h in "abcdefg"]          # 7 sources
order, nxt = C.rotate_sources(srcs, 0, 3)
check("a zero cursor leaves the order alone",
      [s["handle"] for s in order] == list("abcdefg"))
check("the cursor advances by the step", nxt == 3)
order, nxt = C.rotate_sources(srcs, 3, 3)
check("the list starts where the cursor points",
      [s["handle"] for s in order] == list("defgabc"))
check("the cursor wraps", C.rotate_sources(srcs, 6, 3)[1] == 2)
check("a cursor past the end wraps instead of emptying the list",
      len(C.rotate_sources(srcs, 99, 3)[0]) == 7)
check("a missing cursor is treated as zero",
      [s["handle"] for s in C.rotate_sources(srcs, None, 3)[0]] == list("abcdefg"))
check("a junk cursor is treated as zero",
      [s["handle"] for s in C.rotate_sources(srcs, "x", 3)[0]] == list("abcdefg"))
check("a zero step still moves, so runs never repeat the same order",
      C.rotate_sources(srcs, 0, 0)[1] == 1)
check("an empty source list is safe", C.rotate_sources([], 4, 3) == ([], 0))

# every source must reach the front within a full cycle, or rotation is pointless
seen, cur = set(), 0
for _ in range(len(srcs)):
    order, cur = C.rotate_sources(srcs, cur, 3)
    seen.update(s["handle"] for s in order[:3])
check("every source reaches an early slot within one cycle",
      seen == set("abcdefg"), str(sorted(seen)))


# ---------------------------------------------- browser rationing + breaker
print("\n--- browser rationing ---")
C.fetch_with_browser = fake_browser

try:
    calls.clear()
    b = {"browser": 2, "walls": 0, "eligible": None}
    for h in ("a", "b", "c", "d"):
        C.collect_facebook(h, h, 8, cfg, EmptySession(), {}, b)
    check("browser pass stops once the budget is spent", len(calls) == 2, str(calls))
    check("budget counts down to zero", b["browser"] == 0)

    calls.clear()
    b = {"browser": 9, "walls": 0, "eligible": {"yes1", "yes2"}}
    for h in ("yes1", "no1", "yes2", "no2"):
        b["starved"] = 0          # isolate from the throttle breaker
        C.collect_facebook(h, h, 8, cfg, EmptySession(), {}, b)
    check("only rotation-eligible handles get the browser pass",
          [c[0] for c in calls] == ["yes1", "yes2"], str(calls))

    _, st = C.collect_facebook("no3", "no3", 8, cfg, EmptySession(), {},
                               {"browser": 9, "walls": 0, "eligible": {"other"}})
    check("skipped sources say so rather than reporting a failure",
          "rationed" in st["error"], st["error"])

    # three consecutive login walls should trip the breaker
    def walled(*a, **k):
        raise RuntimeError("redirected to a login page — this page is not viewable logged out")
    C.fetch_with_browser = walled
    calls.clear()
    b = {"browser": 9, "walls": 0, "eligible": None}
    for h in ("w1", "w2", "w3", "w4", "w5"):
        b["starved"] = 0          # isolate from the throttle breaker
        C.collect_facebook(h, h, 8, cfg, EmptySession(), {}, b)
    check("breaker trips after three consecutive walls", b["browser"] == 0)
    check("walls counted", b["walls"] >= 3, str(b))

    # a success in between must reset the counter
    C.fetch_with_browser = fake_browser
    b = {"browser": 9, "walls": 2, "eligible": None}
    C.collect_facebook("good", "good", 8, cfg, EmptySession(), {}, b)
    check("a success resets the wall counter", b["walls"] == 0, str(b))
finally:
    C.fetch_with_browser = real_browser

check("default budget lets a lone call through",
      C.collect_facebook("solo", "solo", 8, cfg, OneSession(), {})[1]["ok"] is True)

print("\n" + ("FAILED: " + ", ".join(fails) if fails else "All checks passed."))
sys.exit(1 if fails else 0)
