#!/usr/bin/env python3
"""
Collector: polls 3 fixed sources (muskmeter.live + 2 Nitter instances) on a
fixed stagger within each 60s cycle: :00 muskmeter, :20 nitter A, :40 nitter B.
Appends new posts to the CSV stored in the GitHub repo (via github_store.py)
and optionally sends ntfy notifications.

Log format (one line per event):
    ACTION: outcome (detail)

Behavior (notifications, ntfy topic) is driven by config.json, re-read
every loop iteration so web-UI edits apply live.
"""

import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config_store import load_config
from github_store import read_csv_rows, append_csv_rows

EASTERN = ZoneInfo("America/New_York")

NTFY_BASE = "https://ntfy.sh"
TARGET_USER = "elonmusk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
TIMEOUT_SECONDS = 15

# Fixed 3-source rotation: muskmeter + 2 known-working Nitter instances,
# staggered at :00 :20 :40 within each 60s cycle.
SOURCES = [
    {"id": "muskmeter.live", "kind": "muskmeter", "url": "https://www.muskmeter.live/", "offset": 0},
    {"id": "nitter.kareem.one", "kind": "nitter", "url": f"https://nitter.kareem.one/{TARGET_USER}", "offset": 20},
    {"id": "xcancel.com", "kind": "nitter", "url": f"https://xcancel.com/{TARGET_USER}", "offset": 40},
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


def utc_now():
    return datetime.now(timezone.utc)


def log(action, outcome, detail=""):
    ts = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {action}: {outcome}"
    if detail:
        line += f" ({detail})"
    print(line)


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
    except urllib.error.URLError as e:
        return None, None, "error"


# ---------------------------------------------------------------------------
# muskmeter.live parser
# ---------------------------------------------------------------------------

def extract_post_count_muskmeter(html_content):
    match = re.search(r'Posts 24h \((\d+)\)', html_content)
    return int(match.group(1)) if match else None


def extract_tweets_muskmeter(html_content):
    tweets = []
    now_utc = utc_now()

    for match in re.finditer(
        r'<a href="https://x\.com/elonmusk/status/(\d+)"[^>]*>(.*?)</a>',
        html_content, re.DOTALL,
    ):
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
                    posted_datetime = now_utc - timedelta(days=int(re.search(r'(\d+)d', relative_time).group(1)))
                elif "h" in relative_time:
                    posted_datetime = now_utc - timedelta(hours=int(re.search(r'(\d+)h', relative_time).group(1)))
                elif "m" in relative_time:
                    posted_datetime = now_utc - timedelta(minutes=int(re.search(r'(\d+)m', relative_time).group(1)))
            except (AttributeError, ValueError):
                posted_datetime = None

        tweet_type = "retweet" if ("RT @" in content or "RT @" in tweet_html[:500]) else "direct"
        tweets.append({"id": tweet_id, "content": content, "type": tweet_type, "posted_datetime": posted_datetime})

    return tweets


def parse_muskmeter(html_text):
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    if extract_post_count_muskmeter(html_text) is None:
        return [], False
    return extract_tweets_muskmeter(html_text), True


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
    r'(?:<div class="(?:quote|attachments|card|tweet-stats)|\Z)',
    re.DOTALL,
)
NITTER_DATE_TITLE_RE = re.compile(r'<span class="tweet-date"><a[^>]*title="([^"]*)"')


def _strip_tags(html_fragment):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', html_fragment)).strip()


def _parse_nitter_absolute_timestamp(title_str):
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
        posted_datetime = _parse_nitter_absolute_timestamp(date_match.group(1)) if date_match else None

        tweets.append({
            "id": tweet_id, "content": content,
            "type": "retweet" if is_retweet else "direct",
            "posted_datetime": posted_datetime,
        })
    return tweets


def parse_nitter(html_text):
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    if "timeline-item" not in html_text:
        return [], False
    return extract_tweets_nitter(html_text), True


def parse_source(kind, html_text):
    return parse_muskmeter(html_text) if kind == "muskmeter" else parse_nitter(html_text)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def load_seen_ids():
    header, rows, sha = read_csv_rows()
    log("github.read_csv", "ok", f"rows={len(rows)} sha={sha}")
    return {row[0] for row in rows if row}


def process_fetch_result(source_id, kind, html_text, seen_tweet_ids, cfg):
    tweets, parseable = parse_source(kind, html_text)
    if not parseable:
        return None

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
        log("github.append_csv", "ok", f"source={source_id} rows={len(rows_to_append)}")
    except RuntimeError as e:
        log("github.append_csv", "error", str(e))
        for t in new_posts:
            seen_tweet_ids.discard(t["id"])
        return None

    if cfg.get("notify_every_post", True):
        ntfy_topic = cfg.get("ntfy_topic", "chan6667")
        for t in new_posts[:2]:
            msg = f"{t['type']} post (via {source_id})"
            if t["content"]:
                msg += f"\n{t['content'][:200]}"
            send_ntfy_notification(ntfy_topic, msg, "Elon Musk post", priority=3)

    return len(new_posts)


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

                result = process_fetch_result(source["id"], source["kind"], html_text, seen_tweet_ids, cfg)

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
