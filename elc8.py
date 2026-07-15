#!/usr/bin/env python3
"""
Collector: polls muskmeter.live, extracts new posts, appends them to the
CSV stored in the GitHub repo (via github_store.py), and optionally sends
ntfy notifications. Prints a short success message on every successful
fetch of muskmeter.live.

Behavior is driven by config.json (see config_store.py) which can be edited
live via the web interface -- this script re-reads it every loop iteration.

Run standalone:
    python elc8.py
or import run_collector() from start.py.
"""

import os
import re
import time
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from config_store import load_config
from github_store import read_csv_rows, append_csv_rows, CSV_HEADER

NTFY_BASE = "https://ntfy.sh"
MUSKMETER_URL = "https://www.muskmeter.live/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

EST_OFFSET = timedelta(hours=-4)  # EDT = UTC-4; adjust if standard time matters to you


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
            "Click": "https://www.muskmeter.live/",
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


def fetch(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.status, "ok"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, 429, "rate_limited"
        return None, e.code, "error"
    except urllib.error.URLError as e:
        print(f"[{utc_now_str()}] request error: {e}")
        return None, None, "error"


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_str():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def to_est(dt_utc):
    return dt_utc + EST_OFFSET


def format_datetime_est_from_utc(dt_utc):
    if dt_utc is None:
        return "", "", ""
    est_dt = to_est(dt_utc)
    at_str = est_dt.strftime("%-m/%-d/%Y, %-I:%M:%S %p")
    date_str = est_dt.strftime("%-m/%-d/%Y")
    time_str = est_dt.strftime("%-I:%M:%S %p")
    return at_str, date_str, time_str


def extract_post_count(html_content):
    match = re.search(r'Posts 24h \((\d+)\)', html_content)
    return int(match.group(1)) if match else None


def extract_tweets_with_timestamps(html_content):
    """Returns a list of dicts with id, content, type, relative_time,
    posted_datetime (UTC-aware datetime or None)."""
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
            "relative_time": relative_time,
            "posted_datetime": posted_datetime,
        })

    return tweets


def load_seen_ids():
    """Pull the current CSV from GitHub once at startup to seed the seen-id
    set, so a restart doesn't re-notify on every historical post."""
    header, rows, sha = read_csv_rows()
    id_idx = 0  # 'Tweet ID' is column 0
    seen = set()
    for row in rows:
        if row:
            seen.add(row[id_idx])
    return seen


def run_collector(stop_event=None):
    """Main collector loop. If stop_event (threading.Event) is provided,
    the loop exits cleanly when it is set -- used by start.py to allow
    graceful shutdown of the whole process group."""
    cfg = load_config()
    ntfy_topic = cfg.get("ntfy_topic", "chan6667")

    print("Collector starting, loading seen IDs from GitHub CSV...")
    seen_tweet_ids = load_seen_ids()
    print(f"Loaded {len(seen_tweet_ids)} known tweet IDs.")

    if cfg.get("notify_on_start_stop", True):
        send_ntfy_notification(ntfy_topic, "elc8 collector started", "Collector Started", priority=2)

    headers = {"User-Agent": UA}

    interval = cfg.get("poll_start_interval", 5)
    min_interval = cfg.get("poll_min_interval", 3)
    max_interval = cfg.get("poll_max_interval", 60)
    start_interval = cfg.get("poll_start_interval", 5)
    consecutive_errors = 0

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break

            # Re-read config every iteration so web-UI edits apply live.
            cfg = load_config()
            ntfy_topic = cfg.get("ntfy_topic", "chan6667")
            notify_every_post = cfg.get("notify_every_post", True)
            min_interval = cfg.get("poll_min_interval", min_interval)
            max_interval = cfg.get("poll_max_interval", max_interval)
            start_interval = cfg.get("poll_start_interval", start_interval)

            html, status_code, status = fetch(MUSKMETER_URL, headers)

            if status == "rate_limited":
                interval = min(max_interval, max(interval * 2, 10))
                print(f"[{utc_now_str()}] 429 rate limited -> backing off to {interval:.1f}s")
                time.sleep(interval)
                continue

            if status == "error":
                consecutive_errors += 1
                interval = min(max_interval, interval * 1.5)
                print(f"[{utc_now_str()}] fetch error (consecutive={consecutive_errors})")
                time.sleep(interval)
                if consecutive_errors > 20:
                    print("Too many consecutive errors, stopping collector.")
                    break
                continue

            consecutive_errors = 0
            current_count = extract_post_count(html)
            tweets = extract_tweets_with_timestamps(html)

            if current_count is None:
                print(f"[{utc_now_str()}] fetched OK but could not extract post count")
                time.sleep(interval)
                continue

            print(f"[{utc_now_str()}] OK read muskmeter.live (posts_24h={current_count}, "
                  f"tweets_seen_on_page={len(tweets)})")

            new_posts = []
            now_utc = utc_now()
            imported_at, imported_date, imported_time = format_datetime_est_from_utc(now_utc)

            for tweet in tweets:
                if tweet["id"] in seen_tweet_ids:
                    continue
                seen_tweet_ids.add(tweet["id"])
                new_posts.append(tweet)

            if new_posts:
                rows_to_append = []
                for tweet in new_posts:
                    posted_at, posted_date, posted_time = format_datetime_est_from_utc(tweet["posted_datetime"])
                    rows_to_append.append([
                        tweet["id"], "elonmusk", tweet["content"],
                        posted_at, posted_date, posted_time,
                        imported_at, imported_date, imported_time,
                    ])

                try:
                    append_csv_rows(rows_to_append, commit_message=f"elc8: +{len(rows_to_append)} post(s)")
                    print(f"  wrote {len(rows_to_append)} new row(s) to GitHub CSV")
                except RuntimeError as e:
                    print(f"  [github] write failed: {e}")
                    # Roll back seen-id additions so we retry next loop
                    for tweet in new_posts:
                        seen_tweet_ids.discard(tweet["id"])
                    time.sleep(interval)
                    continue

                for tweet in new_posts:
                    print(f"  NEW {tweet['id']} ({tweet['type']}) posted {tweet['relative_time']} ago")
                    if tweet["content"]:
                        print(f"    {tweet['content'][:100]}")

                if notify_every_post:
                    for tweet in new_posts[:2]:
                        msg = f"{tweet['type']} post"
                        if tweet["content"]:
                            msg += f"\n{tweet['content'][:200]}"
                        send_ntfy_notification(
                            ntfy_topic, msg, f"Elon Musk post #{current_count}", priority=3
                        )

                interval = max(min_interval, interval * 0.5)
            else:
                interval = min(start_interval, interval * 1.1) if interval < start_interval else interval

            time.sleep(interval)

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


if __name__ == "__main__":
    run_collector()
