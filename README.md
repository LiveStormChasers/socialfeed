# Social Feed

A personal X + Facebook aggregator that runs entirely on GitHub. A scheduled
Action collects the latest public posts every few minutes, downloads a preview
image for anything with a photo or video, commits the result, and GitHub Pages
serves the timeline.

Nothing runs on your computer. No API keys, no developer accounts, no tokens
from either platform.

---

## Setup — six steps

**1. Unzip.** You'll get a folder with seven visible files and one hidden folder,
`.github`, which is the part that makes anything run. Show hidden files before
you go further — Mac: `⌘ ⇧ .` in Finder. Windows: File Explorer → View → Show →
Hidden items.

**2. Create the repo.** github.com/new → name it `social-feed` → **Public** →
leave the "initialize this repository with" boxes unchecked. Public matters:
public repos get unlimited free Actions minutes and free Pages. Private repos
meter both.

**3. Upload.** On the empty repo page click *uploading an existing file*. Select
everything *inside* the unzipped folder — including `.github` — and drag it in.
Not the folder itself. Commit.

Then check that `.github/workflows/collect.yml` is in the file list. If it isn't,
`.github` was hidden when you dragged: **Add file → Create new file**, type
`.github/workflows/collect.yml` as the filename (typing the slashes creates the
folders), paste in the contents of that file, commit.

**4. Let Actions write to the repo.** Settings → Actions → General → **Workflow
permissions** → *Read and write permissions* → Save. Without this the collector
runs, finds posts, and then fails at the last moment trying to save them.

**5. Turn on Pages.** Settings → Pages → Source: *Deploy from a branch*,
Branch: **main**, folder: **/ (root)** → Save. Your feed lands at
`https://<your-username>.github.io/social-feed/`.

**6. Run it.** Actions tab → *I understand my workflows, go ahead and enable
them* → **Collect feed** in the sidebar → **Run workflow**.

Give it two or three minutes, then open the run and read the summary at the
bottom: a table of every account, whether it collected, and which route worked.
The same table sits at the top of the feed itself.

**One thing to know:** anyone with the URL can read the feed. GitHub Pages has no
way to put a login in front of it. The link isn't published anywhere, but it
isn't secret either.

---

## How often it runs

Every 5 minutes — the shortest interval GitHub accepts. Because the repo is
public, the runner time is free and unlimited, so there's no cost reason to slow
it down.

Two caveats worth setting expectations on:

**GitHub does not honour it exactly.** Scheduled workflows run on shared
infrastructure and get deprioritised under load. Every 5 minutes in the crontab
usually means every 5–20 in practice, and can stretch further at busy times of
day. Nothing is wrong when this happens.

**The platforms are the real limit, not GitHub.** Polling X and Facebook this
often from a datacenter IP is the most likely thing to get you rate-limited.
Nothing of yours is at risk — no account is involved, the routes are all logged
out — so the worst case is failed runs, not a ban. But if the status table starts
showing failures where it used to show `ok`, back off: change the `cron` line in
`.github/workflows/collect.yml` to `*/15 * * * *` or `*/30 * * * *`.

A run that finds nothing new commits nothing, so a quiet day doesn't fill the
repo with empty commits.

To change the cadence, that one line is the only edit:

```yaml
    - cron: "*/5 * * * *"      # every 5 min (current)
    - cron: "*/15 * * * *"     # every 15 min
    - cron: "0 * * * *"        # hourly
```

---

## Accounts

The watch list is **not in this repo**. It lives in the `SOURCES_JSON` repository
secret, so a public repo does not publish who is being followed. The collector
reads the secret, and falls back to a local `sources.json` (gitignored) if you
are running it by hand.

The secret holds the same JSON the file used to:

```json
{
  "max_posts_per_source": 10,
  "sources": [
    { "platform": "facebook", "handle": "<page>", "label": "<display name>", "enabled": true }
  ]
}
```

`handle` is the part of the URL after the domain; for Facebook you can paste a
full URL instead, which is easier for pages with numeric IDs. `enabled: false`
parks a source without deleting it.

To change the list, edit the secret under **Settings → Secrets and variables →
Actions**. Unlike a file, this does not start a collection by itself — the next
scheduled beat picks it up, or trigger one from the Actions tab.

## Why the feed is encrypted

This repo is public, so everything committed to it is public forever, including
every past version. The feed names every account and carries their posts, so it
is encrypted before it is committed and decrypted in the browser with a
passphrase (`FEED_PASSPHRASE`). Only `feed.enc` is committed; `feed.json` is
gitignored and exists only inside a runner while a run is in progress.

Run logs and job summaries are public too, so sources appear there as short tags
(`src-a1b2c3`) rather than names. The feed's health panel shows each tag beside
its account, which is how a log line is matched back.

Two things this does **not** do: it does not protect what was published before
it was turned on, and — because anyone can download `feed.enc` and guess at it
offline with no rate limit — its strength is entirely the strength of the
passphrase. Use a long random one.

Tuning sits at the top of the same file: `max_posts_per_source`,
`max_feed_items`, `keep_days` (posts older than this drop off and their images
are deleted), `preview_max_width`, `preview_quality`.

---

## How each platform is collected

**No account is needed for either platform.** Public Facebook Pages render for
logged-out visitors, and X's syndication endpoints serve public timelines to
anyone.

**X** — two endpoints, tried in order: the syndication timeline-profile page and
the older CDN timeline JSON. These power embedded timelines on third-party
websites, so they answer without a login and return text, exact timestamps,
photos and video URLs.

**Facebook** — two routes, both logged out:

1. *Public HTML.* A plain fetch of the Page. Post data ships inside JSON blobs in
   the page's `<script>` tags; the collector walks those and pulls out
   post-shaped objects. Fast, no browser.
2. *Logged-out browser.* If route 1 finds nothing, the page renders in headless
   Chromium and posts are read from the DOM. The login modal Facebook overlays on
   a logged-out Page is removed first — the posts are already underneath it.
   Slower, but it survives payload reshuffles that break route 1.

All four endpoints are unofficial and can change without notice, so every source
records *why* it failed rather than letting the feed quietly go stale.

If a Facebook page can't be collected at all, the feed falls back to Facebook's
own Page Plugin embed at the bottom — a live iframe that needs no key, but which
is Facebook's widget rather than your data: not searchable, not archived, not
merged into the timeline.

---

## Optional: cookie secrets

If a platform starts refusing the logged-out routes, a session cookie unlocks a
logged-in browser as a last resort. Repository secrets `FB_COOKIES` /
`X_COOKIES`, accepting a Playwright `storage_state` JSON, a plain cookie array
from a browser export extension, or a `name=value; name=value` header string.

**Not on a public repo.** A secret is safe from readers, but anyone you give
write access to can exfiltrate one through a workflow change — and on a public
repo the workflow file itself is visible to everyone, which makes it an obvious
target. If you ever need this, make the repo private first and accept the metered
minutes, and use a secondary account you would not mind losing.

Try this only if the logged-out routes actually fail. They are better in every
respect when they work.

---

## Things that will come up

**Repo size.** Every preview image is committed, so git history grows even though
`keep_days` prunes the working copy. At 900px JPEG that's roughly 60–120 KB per
post. If it ever matters, delete `media/` in a commit and let it rebuild.

**Scheduled workflows pause.** GitHub disables them after 60 days without repo
activity. The collector commits regularly, so this only bites if every source has
been failing for two months.

**A run that collects nothing still succeeds.** The workflow only fails when
every source failed *and* there's no cached feed, so one platform breaking never
throws away the other's data.

**Times marked `~`.** Facebook's public HTML route carries a real unix timestamp,
so those are exact. The browser routes have no machine-readable time and parse
the visible "5h" or "Yesterday at 4:12 PM" label instead; anything unparseable
falls back to when the collector first saw the post. X is always exact.

**When a parser breaks.** The status table names the route and the error. Both
harvesters are structural rather than path-based — they search the payload for
anything post-shaped instead of following a fixed key path — so a reshuffle does
not automatically break them, but a rename of the fields they key on
(`creation_time`, `message.text`, `id_str`) will. Run
`python tests/test_collect.py` after changing any parser; it covers every payload
shape offline, no network needed.

**Terms of service.** Automated collection is against both platforms' terms even
for public posts. The practical exposure here is an endpoint that stops
answering.

---

## Files

| File | Purpose |
|---|---|
| `collect.py` | The collector. Fetch routes, media, dedupe, pruning. |
| `sources.json` | Accounts and limits. The file you edit. |
| `index.html` | The timeline page Pages serves. |
| `.github/workflows/collect.yml` | Schedule and commit step. |
| `feed.json` | Generated. The feed data. |
| `media/` | Generated. Preview images, downscaled to 900px JPEG. |
| `tests/test_collect.py` | Offline tests — run after changing a parser. |
