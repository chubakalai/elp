#!/usr/bin/env python3
"""
Collector: polls 2 Nitter sources on a fixed stagger within each 60s cycle:
:00 nitter.kareem.one, :30 xcancel.com. Appends new posts to the CSV stored
in the GitHub repo (via github_store.py) and optionally sends ntfy
notifications.

muskmeter.live has been dropped entirely: its relative-time reconstruction
produces lower-precision timestamps (rounded to the minute, :00 seconds)
than Nitter's own precise absolute timestamps, and mixing the two sources
risked a given post's canonical CSV timestamp coming from whichever source
won the race for that polling cycle -- not necessarily the more precise one.
Nitter-only removes this failure mode entirely rather than mitigating it.

Log format (one line per event):
    ACTION: outcome (detail)
"""

import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from config_store import load_config
from github_store import read_csv_rows, append_csv_rows

EASTERN = ZoneInfo("America/New_York")

NTFY_BASE = "https://ntfy.sh"
TARGET_USER = "elonmusk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
TIMEOUT_SECONDS = 15

# Launch time in EST (when the collector was first deployed)
LAUNCH_TIME_EST = datetime(2024, 1, 1, 0, 0, 0, tzinfo=EASTERN)  # Adjust this to actual launch date

# Fixed 2-source rotation, staggered at :00 :30 within each 60s cycle.
SOURCES = [
    {"id": "nitter.kareem.one", "url": f"https://nitter.kareem.one/{TARGET_USER}", "offset": 0},
    {"id": "xcancel.com", "url": f"https://xcancel.com/{TARGET_USER}", "offset": 30},
]

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

_BOILERPLATE_STRINGS = ["Enable hls playback"]


def utc_now():
    return datetime.now(timezone.utc)


def est_now():
    """Return current time in EST."""
    return datetime.now(EASTERN)


def log(action, outcome, detail=""):
    ts = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {action}: {outcome}"
    if detail:
        line += f" ({detail})"
    print(line)


def to_est(dt_utc):
    return dt_utc.astimezone(EASTERN)


def format_datetime_est(dt_est):
    """Format an EST datetime into display strings."""
    if dt_est is None:
        return "", "", ""
    at_str = dt_est.strftime("%-m/%-d/%Y, %-I:%M:%S %p")
    date_str = dt_est.strftime("%-m/%-d/%Y")
    time_str = dt_est.strftime("%-I:%M:%S %p")
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
        headers={"Title": title, "Priority": str(priority), "Tags": "chart_with_upwards_trend,robot_face"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log("ntfy", "sent", f"topic={topic} title={title!r}")
            else:
                log("ntfy", "failed", f"status={resp.status}")
    except urllib.error.URLError as e:
        log("ntfy", "error", str(e))


def fetch(url, timeout=TIMEOUT_SECONDS):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), "ok"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return 429, None, "rate_limited"
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, "error"
    except urllib.error.URLError:
        return None, None, "error"


# ---------------------------------------------------------------------------
# Nitter parser
# ---------------------------------------------------------------------------

NITTER_ITEM_RE = re.compile(
    r'<div class="timeline-item[^"]*"\s+data-username="([^"]*)">(.*?)'
    r'(?=<div class="timeline-item|<div class="show-more"|\Z)',
    re.DOTALL,
)
NITTER_LINK_RE = re.compile(r'<a class="tweet-link" href="/[^/]+/status/(\d+)')
NITTER_RETWEET_HEADER_RE = re.compile(r'<div class="retweet-header">')
NITTER_CONTENT_RE = re.compile(
    r'<div class="tweet-content media-body"[^>]*>(.*?)</div>\s*'
    r'(?=<div class="(?:quote|attachments|card|tweet-stats)|\Z)',
    re.DOTALL,
)
NITTER_DATE_TITLE_RE = re.compile(r'<span class="tweet-date"><a[^>]*title="([^"]*)"')


def _strip_tags(html_fragment):
    text = re.sub(r'<[^>]+>', '', html_fragment)
    text = re.sub(r'\s+', ' ', text).strip()
    for boilerplate in _BOILERPLATE_STRINGS:
        text = text.replace(boilerplate, "").strip()
    return text


def _parse_nitter_absolute_timestamp(title_str):
    """Parse Nitter's UTC timestamp string to UTC datetime."""
    cleaned = re.sub(r'\s+', ' ', title_str.replace("\u00b7", "").strip())
    try:
        return datetime.strptime(cleaned, "%b %d, %Y %I:%M %p UTC").replace(tzinfo=timezone.utc)
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
        posted_datetime_utc = _parse_nitter_absolute_timestamp(date_match.group(1)) if date_match else None

        tweets.append({
            "id": tweet_id, 
            "content": content,
            "type": "retweet" if is_retweet else "direct",
            "posted_datetime_utc": posted_datetime_utc,  # Original post time (UTC)
            "is_retweet": is_retweet,
        })
    return tweets


def parse_nitter(html_text):
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    if "timeline-item" not in html_text:
        return [], False
    return extract_tweets_nitter(html_text), True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def load_seen_ids():
    header, rows, sha = read_csv_rows()
    log("github.read_csv", "ok", f"rows={len(rows)} sha={sha}")
    return {row[0] for row in rows if row}


def is_valid_post(posted_datetime_est, parsing_time_est):
    """Check if post should be included:
    - Must be after launch time
    - Must not be older than 10 minutes from parsing time
    - For retweets: we use parsing time, so they're always within 10 min window
    - For direct posts: check original timestamp is within 10 min
    """
    if posted_datetime_est < LAUNCH_TIME_EST:
        return False
    
    # Check if post is older than 10 minutes
    age = parsing_time_est - posted_datetime_est
    if age > timedelta(minutes=10):
        return False
    
    return True


def process_fetch_result(source_id, html_text, seen_tweet_ids, cfg):
    tweets, parseable = parse_nitter(html_text)
    if not parseable:
        return None

    # Record the parsing time in EST (this is the time we discovered the post)
    parsing_time_est = est_now()

    # Filter out already seen tweets
    new_posts = [t for t in tweets if t["id"] not in seen_tweet_ids]
    if not new_posts:
        return 0

    # Re-verify against GitHub directly before writing, in case seen_tweet_ids
    # drifted from the authoritative CSV (e.g. after a prior write failure).
    try:
        _header, existing_rows, _sha = read_csv_rows()
        existing_ids = {row[0] for row in existing_rows if row}
        seen_tweet_ids.update(existing_ids)
    except RuntimeError:
        pass

    new_posts = [t for t in new_posts if t["id"] not in seen_tweet_ids]
    if not new_posts:
        return 0

    rows_to_append = []
    valid_new_posts = []
    
    for t in new_posts:
        # Determine the effective posted datetime in EST
        if t["is_retweet"]:
            # For retweets: use the parsing time as the posted time
            # because the original timestamp is when the original post was made,
            # not when Elon retweeted it
            effective_posted_est = parsing_time_est
            log("post.process", "retweet_adjusted", 
                f"id={t['id']} using_parse_time={effective_posted_est.strftime('%m/%d/%Y %I:%M:%S %p')}")
        else:
            # For direct posts: convert original UTC timestamp to EST
            if t["posted_datetime_utc"]:
                effective_posted_est = to_est(t["posted_datetime_utc"])
            else:
                log("post.process", "skip_no_timestamp", f"id={t['id']}")
                continue
        
        # Check if post passes validation
        if not is_valid_post(effective_posted_est, parsing_time_est):
            log("post.process", "skip_out_of_window", 
                f"id={t['id']} type={t['type']} posted={effective_posted_est.strftime('%m/%d/%Y %I:%M:%S %p')}")
            continue
        
        valid_new_posts.append(t)
        
        # Format the EST datetime for CSV
        posted_at, posted_date, posted_time = format_datetime_est(effective_posted_est)
        imported_at, imported_date, imported_time = format_datetime_est(parsing_time_est)
        
        rows_to_append.append([
            t["id"], "elonmusk", t["content"],
            posted_at, posted_date, posted_time,
            imported_at, imported_date, imported_time,
        ])

    if not valid_new_posts:
        return 0

    # Mark these posts as seen
    for t in valid_new_posts:
        seen_tweet_ids.add(t["id"])

    try:
        append_csv_rows(rows_to_append, commit_message=f"elc8 ({source_id}): +{len(rows_to_append)} post(s)")
        log("github.append_csv", "ok", f"source={source_id} rows={len(rows_to_append)}")
    except RuntimeError as e:
        log("github.append_csv", "error", str(e))
        for t in valid_new_posts:
            seen_tweet_ids.discard(t["id"])
        return None

    if cfg.get("notify_every_post", True):
        ntfy_topic = cfg.get("ntfy_topic", "chan6667")
        for t in valid_new_posts[:2]:
            msg = f"{t['type']} post (via {source_id})"
            if t["content"]:
                msg += f"\n{t['content'][:200]}"
            send_ntfy_notification(ntfy_topic, msg, "Elon Musk post", priority=3)

    return len(valid_new_posts)


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


def run_collector(stop_event=None):
    cfg = load_config()
    ntfy_topic = cfg.get("ntfy_topic", "chan6667")

    seen_tweet_ids = load_seen_ids()
    log("collector.start", "ok", f"known_ids={len(seen_tweet_ids)}")
    log("collector.config", "launch_time", f"{LAUNCH_TIME_EST.strftime('%m/%d/%Y %I:%M:%S %p')} EST")

    if cfg.get("notify_on_start_stop", True):
        send_ntfy_notification(ntfy_topic, "elc8 collector started", "Collector Started", priority=2)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break

            cfg = load_config()
            cycle_start = time.monotonic()

            for source in SOURCES:
                if stop_event is not None and stop_event.is_set():
                    break

                target_time = cycle_start + source["offset"]
                now = time.monotonic()
                if target_time > now:
                    _sleep_interruptible(target_time - now, stop_event)

                status, body, fetch_status = fetch(source["url"])

                if fetch_status == "rate_limited":
                    log("source.fetch", "rate_limited", source["id"])
                    continue
                if fetch_status != "ok" or body is None:
                    log("source.fetch", "error", f"{source['id']} status={status}")
                    continue

                try:
                    html_text = body.decode("utf-8", errors="replace")
                except Exception as e:
                    log("source.fetch", "decode_error", f"{source['id']} {e}")
                    continue

                result = process_fetch_result(source["id"], html_text, seen_tweet_ids, cfg)

                if result is None:
                    log("source.fetch", "unparseable", f"{source['id']} (challenge page or structure mismatch)")
                else:
                    log("source.fetch", "ok", f"{source['id']} new_posts={result}")

            elapsed = time.monotonic() - cycle_start
            remaining = 60 - elapsed
            if remaining > 0:
                _sleep_interruptible(remaining, stop_event)

    except KeyboardInterrupt:
        pass
    finally:
        log("collector.stop", "ok", f"total_known_ids={len(seen_tweet_ids)}")
        cfg = load_config()
        if cfg.get("notify_on_start_stop", True):
            send_ntfy_notification(
                cfg.get("ntfy_topic", "chan6667"),
                f"elc8 collector stopped. Total posts: {len(seen_tweet_ids)}",
                "Collector Stopped", priority=2,
            )


if __name__ == "__main__":
    run_collector()
