#!/usr/bin/env python3
"""
Collector: polls a rotating pool of sources (muskmeter.live + Nitter
instances) for elonmusk's posts, appends new posts to the CSV stored in
the GitHub repo (via github_store.py), and optionally sends ntfy
notifications. Prints a short success message on every successful fetch.

Source health tracking:
  - A persistent "healthy pool" of sources that have at least once yielded
    parseable content is stored at /data/healthy_sources.json.
  - Every 5 minutes, candidates NOT currently in the healthy pool are
    (re-)probed; a parseable response promotes them into the pool.
  - A healthy source that fails to yield parseable content for a
    continuous 5 minutes is evicted from the pool.
  - Challenge/interstitial pages (Anubis, Cloudflare, etc.) are treated
    as an ordinary parse failure, not a special instant-eviction signal.

Rotation:
  - With N healthy sources, one is polled every (60 / N) seconds, at fixed
    stagger positions within each 60-second cycle (e.g. N=6 -> :00 :10 :20
    :30 :40 :50). Timing is strict -- a new-post hit does not trigger an
    early extra poll.
  - With 0 healthy sources, the loop still runs the 5-minute discovery
    probe against all candidates; no fetch content occurs until at least
    one becomes healthy.

Behavior (notifications, ntfy topic) is driven by config.json (see
config_store.py), re-read every loop iteration so web-UI edits apply
live.

Run standalone:
    python elc8.py
or import run_collector() from start.py.
"""

import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config_store import load_config
from github_store import read_csv_rows, append_csv_rows, CSV_HEADER

EASTERN = ZoneInfo("America/New_York")

DATA_DIR = os.environ.get("MUSKMETER_DATA_DIR", "/data")
HEALTHY_SOURCES_PATH = os.path.join(DATA_DIR, "healthy_sources.json")

NTFY_BASE = "https://ntfy.sh"
TARGET_USER = "elonmusk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

TIMEOUT_SECONDS = 15

DISCOVERY_INTERVAL_SECONDS = 5 * 60
EVICTION_WINDOW_SECONDS = 5 * 60

# Each candidate source: a unique id, a kind ("muskmeter" or "nitter"), and
# the URL to fetch. Nitter instances all share the same parser; muskmeter
# has its own dedicated one.
NITTER_INSTANCES = [
    "nitter.net",
    "nitter.catsarch.com",
    "nitter.tiekoetter.com",
    "nitter.kareem.one",
    "xcancel.com",
    "nitter.privacyredirect.com",
    "nitter.space",
    "lightbrd.com",
    "nuku.trabun.org",
    "nitter.poast.org",
]

CANDIDATES = [{"id": "muskmeter.live", "kind": "muskmeter",
               "url": "https://www.muskmeter.live/"}]
for _inst in NITTER_INSTANCES:
    CANDIDATES.append({
        "id": _inst, "kind": "nitter",
        "url": f"https://{_inst}/{TARGET_USER}",
    })

CHALLENGE_MARKERS = [
    "making sure you're not a bot",
    "protected by anubis",
    "checking your browser",
    "just a moment",
    "cf-browser-verification",
    "cf_chl_",
    "attention required",
    "ddos protection by",
    "captcha-delivery.com",
    'id="anubis',
]


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_str():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def to_est(dt_utc):
    return dt_utc.astimezone(EASTERN)


def format_datetime_est_from_utc(dt_utc):
    if dt_utc is None:
        return "", "", ""
    est_dt = to_est(dt_utc)
    at_str = est_dt.strftime("%-m/%-d/%Y, %-I:%M:%S %p")
    date_str = est_dt.strftime("%-m/%-d/%Y")
    time_str = est_dt.strftime("%-I:%M:%S %p")
    return at_str, date_str, time_str


def is_challenge_page(text_lower):
    return any(marker in text_lower for marker in CHALLENGE_MARKERS)


def send_ntfy_notification(topic, message, title="New Post Detected!", priority=3):
    if not topic:
        return
    url = f"{NTFY_BASE}/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": str(priority),
            "Tags": "chart_with_upwards_trend,robot_face",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("  [notify] sent")
            else:
                print(f"  [notify] failed: {resp.status}")
    except urllib.error.URLError as e:
        print(f"  [notify] error: {e}")


def fetch(url, timeout=TIMEOUT_SECONDS):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, "ok"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return 429, None, "rate_limited"
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, "error"
    except urllib.error.URLError as e:
        print(f"[{utc_now_str()}] request error for {url}: {e}")
        return None, None, "error"


# ---------------------------------------------------------------------------
# muskmeter.live parser (existing logic)
# ---------------------------------------------------------------------------

def extract_post_count_muskmeter(html_content):
    match = re.search(r'Posts 24h \((\d+)\)', html_content)
    return int(match.group(1)) if match else None


def extract_tweets_muskmeter(html_content):
    """Returns a list of dicts with id, content, type, posted_datetime (UTC)."""
    tweets = []
    now_utc = utc_now()

    tweet_blocks = re.finditer(
        r'<a href="https://x\.com/elonmusk/status/(\d+)"[^>]*>(.*?)</a>',
        html_content,
        re.DOTALL,
    )

    for match in tweet_blocks:
        tweet_id = match.group(1)
        tweet_html = match.group(2)

        content = ""
        content_match = re.search(r'<p class="tweet-content[^"]*">(.*?)</p>', tweet_html, re.DOTALL)
        if content_match:
            content = re.sub(r'<[^>]+>', '', content_match.group(1)).strip()
            content = re.sub(r'\s+', ' ', content).strip()

        if not content:
            rt_match = re.search(r'<p class="rt-text[^"]*">(.*?)</p>', tweet_html, re.DOTALL)
            if rt_match:
                content = re.sub(r'<[^>]+>', '', rt_match.group(1)).strip()
                content = re.sub(r'\s+', ' ', content).strip()

        date_match = re.search(r'<span class="date[^"]*">(.*?)</span>', tweet_html, re.DOTALL)
        relative_time = date_match.group(1).strip() if date_match else ""

        posted_datetime = None
        if relative_time:
            try:
                if relative_time in ("now", "Just now"):
                    posted_datetime = now_utc
                elif "d" in relative_time:
                    days = int(re.search(r'(\d+)d', relative_time).group(1))
                    posted_datetime = now_utc - timedelta(days=days)
                elif "h" in relative_time:
                    hours = int(re.search(r'(\d+)h', relative_time).group(1))
                    posted_datetime = now_utc - timedelta(hours=hours)
                elif "m" in relative_time:
                    minutes = int(re.search(r'(\d+)m', relative_time).group(1))
                    posted_datetime = now_utc - timedelta(minutes=minutes)
            except (AttributeError, ValueError):
                posted_datetime = None

        tweet_type = "retweet" if ("RT @" in content or "RT @" in tweet_html[:500]) else "direct"

        tweets.append({
            "id": tweet_id,
            "content": content,
            "type": tweet_type,
            "posted_datetime": posted_datetime,
        })

    return tweets


def parse_muskmeter(html_text):
    """Returns (tweets_list, parseable_bool). parseable is True if the page
    structure was recognized at all (post count found), even if 0 tweets
    were extracted (e.g. genuinely no new posts)."""
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    count = extract_post_count_muskmeter(html_text)
    if count is None:
        return [], False
    tweets = extract_tweets_muskmeter(html_text)
    return tweets, True
    # ---------------------------------------------------------------------------
# Nitter parser
#
# Structure (see nitter_nitter_kareem_one_elonmusk.html):
#   <div class="timeline-item ..." data-username="elonmusk">
#     <a class="tweet-link" href="/elonmusk/status/<id>#m"></a>
#     <div class="tweet-body">
#       <div>
#         [optional] <div class="retweet-header">...retweeted</div>
#         <div class="tweet-header">...
#           <span class="tweet-date"><a ... title="Jul 15, 2026 · 1:11 PM UTC">14h</a></span>
#         </div>
#       </div>
#       <div class="tweet-content media-body" dir="auto">TEXT</div>
#       [optional nested quote block -- must be excluded from TEXT capture]
#       <div class="tweet-stats">...</div>
#     </div>
#   </div>
#
# data-username on the outer timeline-item tells us whose item this is --
# if it isn't "elonmusk", it's a retweet by definition (Nitter shows
# retweets under the followed account's timeline but data-username is the
# ORIGINAL author). tweet-content is a direct sibling-level div (not
# nested inside quote-big), so a non-greedy match up to the next top-level
# div boundary keeps quoted text out.
# ---------------------------------------------------------------------------

NITTER_ITEM_RE = re.compile(
    r'<div class="timeline-item[^"]*"\s+data-username="([^"]*)">(.*?)'
    r'(?=<div class="timeline-item|<div class="show-more"|\Z)',
    re.DOTALL,
)
NITTER_LINK_RE = re.compile(r'<a class="tweet-link" href="/[^/]+/status/(\d+)')
NITTER_RETWEET_HEADER_RE = re.compile(r'<div class="retweet-header">')
NITTER_CONTENT_RE = re.compile(
    r'<div class="tweet-content media-body"[^>]*>(.*?)</div>\s*(?:<div class="(?:quote|attachments|card|tweet-stats))',
    re.DOTALL,
)
NITTER_DATE_TITLE_RE = re.compile(
    r'<span class="tweet-date"><a[^>]*title="([^"]*)"',
)


def _strip_tags(html_fragment):
    text = re.sub(r'<[^>]+>', '', html_fragment)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _parse_nitter_absolute_timestamp(title_str):
    """title looks like 'Jul 15, 2026 · 1:11 PM UTC'. Returns aware UTC
    datetime, or None if it doesn't parse."""
    cleaned = title_str.replace("\u00b7", "").strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    try:
        naive = datetime.strptime(cleaned, "%b %d, %Y %I:%M %p UTC")
        return naive.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_tweets_nitter(html_content, target_user=TARGET_USER):
    tweets = []

    for item_match in NITTER_ITEM_RE.finditer(html_content):
        item_username = item_match.group(1)
        item_html = item_match.group(2)

        link_match = NITTER_LINK_RE.search(item_html)
        if not link_match:
            continue
        tweet_id = link_match.group(1)

        is_retweet = bool(NITTER_RETWEET_HEADER_RE.search(item_html)) or (
            item_username.lower() != target_user.lower()
        )

        content = ""
        content_match = NITTER_CONTENT_RE.search(item_html)
        if content_match:
            content = _strip_tags(content_match.group(1))

        date_match = NITTER_DATE_TITLE_RE.search(item_html)
        posted_datetime = None
        if date_match:
            posted_datetime = _parse_nitter_absolute_timestamp(date_match.group(1))

        tweets.append({
            "id": tweet_id,
            "content": content,
            "type": "retweet" if is_retweet else "direct",
            "posted_datetime": posted_datetime,
        })

    return tweets


def parse_nitter(html_text):
    """Returns (tweets_list, parseable_bool)."""
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    if "timeline-item" not in html_text:
        return [], False
    tweets = extract_tweets_nitter(html_text)
    return tweets, True


def parse_source(kind, html_text):
    if kind == "muskmeter":
        return parse_muskmeter(html_text)
    return parse_nitter(html_text)


# ---------------------------------------------------------------------------
# Health-pool persistence
#
# State shape on disk:
# {
#   "healthy": {"<source_id>": {"since": <iso>, "last_success": <iso>}, ...},
#   "last_discovery_probe": <iso or null>
# }
# ---------------------------------------------------------------------------

def _default_health_state():
    return {"healthy": {}, "last_discovery_probe": None}


def load_health_state():
    if not os.path.exists(HEALTHY_SOURCES_PATH):
        return _default_health_state()
    try:
        with open(HEALTHY_SOURCES_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or "healthy" not in state:
            return _default_health_state()
        return state
    except (json.JSONDecodeError, OSError):
        return _default_health_state()


def save_health_state(state):
    parent = os.path.dirname(HEALTHY_SOURCES_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    tmp_path = HEALTHY_SOURCES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, HEALTHY_SOURCES_PATH)


def mark_success(state, source_id):
    now_iso = utc_now().isoformat()
    entry = state["healthy"].get(source_id)
    if entry is None:
        entry = {"since": now_iso, "last_success": now_iso}
        print(f"  [health] promoting '{source_id}' to healthy pool")
    else:
        entry["last_success"] = now_iso
    state["healthy"][source_id] = entry


def evict_stale(state):
    """Drop any healthy source whose last_success is older than the
    eviction window."""
    now = utc_now()
    to_drop = []
    for source_id, entry in state["healthy"].items():
        try:
            last_success = datetime.fromisoformat(entry["last_success"])
        except (KeyError, ValueError):
            to_drop.append(source_id)
            continue
        if (now - last_success).total_seconds() > EVICTION_WINDOW_SECONDS:
            to_drop.append(source_id)
    for source_id in to_drop:
        print(f"  [health] evicting '{source_id}' (no parseable result for "
              f"{EVICTION_WINDOW_SECONDS // 60} min)")
        del state["healthy"][source_id]


def should_run_discovery(state):
    last = state.get("last_discovery_probe")
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (utc_now() - last_dt).total_seconds() >= DISCOVERY_INTERVAL_SECONDS


def candidate_by_id(source_id):
    for c in CANDIDATES:
        if c["id"] == source_id:
            return c
    return None


def run_discovery_probe(state):
    """Probe every candidate not currently in the healthy pool; promote on
    a parseable response. Always updates last_discovery_probe regardless
    of outcome, so this runs on a fixed cadence."""
    print(f"[{utc_now_str()}] running discovery probe...")
    for candidate in CANDIDATES:
        source_id = candidate["id"]
        if source_id in state["healthy"]:
            continue
        status, body, fetch_status = fetch(candidate["url"])
        if fetch_status != "ok" or body is None:
            print(f"  [discovery] '{source_id}': unreachable/error, still unhealthy")
            continue
        try:
            html_text = body.decode("utf-8", errors="replace")
        except Exception:
            continue
        _tweets, parseable = parse_source(candidate["kind"], html_text)
        if parseable:
            mark_success(state, source_id)
        else:
            print(f"  [discovery] '{source_id}': reachable but not parseable, still unhealthy")
    state["last_discovery_probe"] = utc_now().isoformat()
    save_health_state(state)


def compute_stagger_schedule(healthy_ids):
    """Given a list of healthy source ids, returns list of (offset_seconds,
    source_id) evenly spread across a 60-second cycle."""
    n = len(healthy_ids)
    if n == 0:
        return []
    step = 60.0 / n
    return [(round(i * step, 3), source_id) for i, source_id in enumerate(healthy_ids)]


# ---------------------------------------------------------------------------
# Main collector loop
# ---------------------------------------------------------------------------

def load_seen_ids():
    header, rows, sha = read_csv_rows()
    seen = set()
    for row in rows:
        if row:
            seen.add(row[0])
    return seen


def process_fetch_result(source_id, kind, html_text, seen_tweet_ids, cfg):
    """Parses one fetched page, appends any new posts to GitHub, sends
    notifications as configured. Returns number of new posts found."""
    tweets, parseable = parse_source(kind, html_text)
    if not parseable:
        return None  # signals failure to caller (for health tracking)

    new_posts = [t for t in tweets if t["id"] not in seen_tweet_ids]
    if not new_posts:
        return 0

    for t in new_posts:
        seen_tweet_ids.add(t["id"])

    now_utc = utc_now()
    imported_at, imported_date, imported_time = format_datetime_est_from_utc(now_utc)

    rows_to_append = []
    for t in new_posts:
        posted_at, posted_date, posted_time = format_datetime_est_from_utc(t["posted_datetime"])
        rows_to_append.append([
            t["id"], "elonmusk", t["content"],
            posted_at, posted_date, posted_time,
            imported_at, imported_date, imported_time,
        ])

    try:
        append_csv_rows(rows_to_append, commit_message=f"elc8 ({source_id}): +{len(rows_to_append)} post(s)")
    except RuntimeError as e:
        print(f"  [github] write failed: {e}")
        for t in new_posts:
            seen_tweet_ids.discard(t["id"])
        return None

    for t in new_posts:
        print(f"  NEW {t['id']} ({t['type']}) via {source_id}")
        if t["content"]:
            print(f"    {t['content'][:100]}")

    if cfg.get("notify_every_post", True):
        ntfy_topic = cfg.get("ntfy_topic", "chan6667")
        for t in new_posts[:2]:
            msg = f"{t['type']} post (via {source_id})"
            if t["content"]:
                msg += f"\n{t['content'][:200]}"
            send_ntfy_notification(ntfy_topic, msg, "Elon Musk post", priority=3)

    return len(new_posts)


def run_collector(stop_event=None):
    cfg = load_config()
    ntfy_topic = cfg.get("ntfy_topic", "chan6667")

    print("Collector starting, loading seen IDs from GitHub CSV...")
    seen_tweet_ids = load_seen_ids()
    print(f"Loaded {len(seen_tweet_ids)} known tweet IDs.")

    state = load_health_state()
    print(f"Healthy pool at startup: {sorted(state['healthy'].keys()) or '(empty)'}")

    if cfg.get("notify_on_start_stop", True):
        send_ntfy_notification(ntfy_topic, "elc8 collector started", "Collector Started", priority=2)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break

            cfg = load_config()

            if should_run_discovery(state):
                run_discovery_probe(state)

            evict_stale(state)
            save_health_state(state)

            healthy_ids = sorted(state["healthy"].keys())
            schedule = compute_stagger_schedule(healthy_ids)

            if not schedule:
                print(f"[{utc_now_str()}] no healthy sources yet, waiting for next discovery probe...")
                _sleep_interruptible(min(30, DISCOVERY_INTERVAL_SECONDS), stop_event)
                continue

            cycle_start = time.monotonic()
            for offset, source_id in schedule:
                if stop_event is not None and stop_event.is_set():
                    break

                target_time = cycle_start + offset
                now = time.monotonic()
                if target_time > now:
                    _sleep_interruptible(target_time - now, stop_event)

                candidate = candidate_by_id(source_id)
                if candidate is None:
                    continue

                status, body, fetch_status = fetch(candidate["url"])

                if fetch_status == "rate_limited":
                    print(f"[{utc_now_str()}] {source_id}: 429 rate limited")
                    continue
                if fetch_status != "ok" or body is None:
                    print(f"[{utc_now_str()}] {source_id}: fetch error")
                    continue

                try:
                    html_text = body.decode("utf-8", errors="replace")
                except Exception:
                    continue

                result = process_fetch_result(source_id, candidate["kind"], html_text, seen_tweet_ids, cfg)

                if result is None:
                    print(f"[{utc_now_str()}] {source_id}: OK read, but not parseable "
                          f"(challenge page or structure mismatch)")
                else:
                    mark_success(state, source_id)
                    print(f"[{utc_now_str()}] OK read {source_id} (+{result} new post(s))")

            save_health_state(state)

    except KeyboardInterrupt:
        pass
    finally:
        print(f"Collector stopped. Total unique posts tracked: {len(seen_tweet_ids)}")
        cfg = load_config()
        if cfg.get("notify_on_start_stop", True):
            send_ntfy_notification(
                cfg.get("ntfy_topic", "chan6667"),
                f"elc8 collector stopped. Total posts: {len(seen_tweet_ids)}",
                "Collector Stopped", priority=2,
            )


def _sleep_interruptible(seconds, stop_event):
    if seconds <= 0:
        return
    if stop_event is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return
        time.sleep(min(0.5, end - time.monotonic()))


if __name__ == "__main__":
    run_collector()
