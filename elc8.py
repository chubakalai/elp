#!/usr/bin/env python3
"""
Collector: 3 fixed-schedule sources per 60s cycle.
  :00  nitter.kareem.one  -> direct posts (Nitter has no retweet-event ID,
                              so retweet extraction from Nitter is disabled)
  :15  xcancel.com        -> direct posts (second Nitter mirror, same role)
  :30  muskmeter.live     -> retweets ONLY (direct-post extraction from
                              muskmeter is disabled -- it previously caused
                              duplicate/low-precision writes racing against
                              Nitter). Muskmeter's URLs are namespaced under
                              /elonmusk/status/<id> regardless of retweet
                              status, giving a retweet-specific ID Nitter
                              cannot provide.

Muskmeter's relative timestamps ("Xm", "Xh") are whole-unit granularity, so
multiple retweets can collide into the same reported minute. When that
happens, they are assumed evenly spaced within that minute:
    offset(i, N) = i * 60 / (N + 1),  i = 1..N
with i=1 = oldest of the batch (earliest offset within the minute) and
i=N = most recent (latest offset), since page order (top = most recent)
determines relative recency within the batch.

Appends new posts to the CSV stored in the GitHub repo (via
github_store.py) and optionally sends ntfy notifications.

Log format (one line per event):
    ACTION: outcome (detail)

Fixes applied vs. prior revision:
  1. Each source's fetch+process step is now wrapped in a broad
     try/except inside the per-cycle loop, so an unexpected exception
     (e.g. a bare socket.timeout / TimeoutError that urllib does not
     wrap in URLError) logs an error for that source and continues to
     the next source, instead of silently killing the whole collector
     thread. A "source.fetch: start" line is also emitted before each
     fetch begins, so a source that produces no further log line is
     immediately diagnosable as "hung mid-fetch" rather than invisible.
  2. Nitter direct-post extraction now also checks the post's own text
     for a leading "RT @" marker. Some mirrors omit the retweet-header
     DOM marker for self-retweets/reposts of the account's own prior
     tweet, which previously caused those items to be misclassified as
     direct posts and mis-attributed to whichever nitter mirror fetched
     them. Content is now the deciding signal in addition to the DOM
     marker and username check.
  3. Muskmeter retweet extraction previously trusted the numeric ID
     embedded in the `/elonmusk/status/<id>` anchor as the sole dedup
     key. In practice Muskmeter has been observed to occasionally
     render the same underlying retweet event under two different
     anchor IDs within the same page fetch (or across two fetches
     moments apart), which produced two CSV rows and two notifications
     for what is, to a human reader, "the same retweet" -- same
     content, timestamps within a few tens of seconds of each other
     (the synthetic even-spacing timestamp is itself unstable between
     fetches, since it depends on page_order and now_utc, both of
     which can shift cycle-to-cycle for a retweet that remains visible
     across multiple polls). A secondary dedup pass has been added
     that, for retweet-type items only, suppresses a new item if its
     normalized content exactly matches a retweet already present in
     the CSV within RETWEET_DUP_WINDOW_SECONDS of its synthetic
     timestamp. This runs in addition to (not instead of) the existing
     ID-based dedup, and is logged distinctly
     ("duplicate_content_suppressed") so a recurrence is immediately
     attributable to this path rather than requiring re-investigation.
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

# Content-level dedup tolerance for retweet-type items. See fix note #3
# in the module docstring. This is deliberately wider than the 60s
# muskmeter bucket width, because the synthetic even-spacing timestamp
# assigned to a given retweet is not stable across fetch cycles (it
# depends on now_utc and the item's page_order, both of which can shift
# while the retweet is still visible on muskmeter's page across polls).
RETWEET_DUP_WINDOW_SECONDS = 90

SOURCES = [
    {"id": "nitter.kareem.one", "kind": "nitter_direct",
     "url": f"https://nitter.kareem.one/{TARGET_USER}", "offset": 0},
    {"id": "xcancel.com", "kind": "nitter_direct",
     "url": f"https://xcancel.com/{TARGET_USER}", "offset": 15},
    {"id": "muskmeter.live", "kind": "muskmeter_retweets",
     "url": "https://www.muskmeter.live/", "offset": 30},
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

# Content-level retweet signal, used as a fallback/override for whatever
# the DOM-marker heuristic (retweet-header div / username mismatch)
# concludes. Nitter mirrors do not consistently emit a retweet-header
# div for self-retweets (the account retweeting its own earlier post),
# so relying on the DOM marker alone under-detects those cases.
RT_PREFIX_RE = re.compile(r'^\s*RT\s+@\w+:', re.IGNORECASE)


def utc_now():
    return datetime.now(timezone.utc)


def log(action, outcome, detail=""):
    ts = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {action}: {outcome}"
    if detail:
        line += f" ({detail})"
    print(line, flush=True)


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


def parse_posted_at_est_to_utc(posted_at_str):
    """Inverse of format_datetime_est_from_utc's 'at_str' field. Returns
    an aware UTC datetime, or None if the string cannot be parsed. Used
    only by the content-dedup pass to compare a candidate new item's
    synthetic timestamp against timestamps already recorded in the CSV."""
    if not posted_at_str:
        return None
    try:
        naive = datetime.strptime(posted_at_str.strip(), "%m/%d/%Y, %I:%M:%S %p")
    except ValueError:
        return None
    return naive.replace(tzinfo=EASTERN).astimezone(timezone.utc)


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
    except Exception as e:
        log("ntfy", "error", f"unexpected: {e!r}")


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
    except TimeoutError:
        # socket.timeout / TimeoutError can surface here without being
        # wrapped in URLError depending on where in the connection the
        # timeout fires (e.g. mid-read after the connection was already
        # established). Treat it the same as a URLError-based timeout.
        return None, None, "error"


# ---------------------------------------------------------------------------
# Nitter parser (direct posts only)
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
    cleaned = re.sub(r'\s+', ' ', title_str.replace("\u00b7", "").strip())
    try:
        return datetime.strptime(cleaned, "%b %d, %Y %I:%M %p UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_direct_posts_nitter(html_content, target_user=TARGET_USER):
    """Returns only DIRECT posts (data-username == target_user AND no
    retweet-header AND content does not start with 'RT @user:'). Retweets
    are skipped entirely here -- muskmeter owns retweet extraction, since
    Nitter cannot provide a retweet-specific ID or timestamp (its
    anchor/date always reflect the ORIGINAL tweet).

    The retweet-header DOM marker is checked first as before, but some
    Nitter mirrors omit that marker specifically for self-retweets (the
    account retweeting its own earlier tweet), because from that mirror's
    perspective the "author" of the retweeted item and the timeline owner
    are the same account. Content is therefore also checked: any item
    whose visible text begins with "RT @<user>:" is treated as a retweet
    regardless of what the DOM marker says, since that prefix is Nitter's
    own textual retweet convention and is unambiguous.
    """
    posts = []
    for item_match in NITTER_ITEM_RE.finditer(html_content):
        item_username = item_match.group(1)
        item_html = item_match.group(2)

        has_retweet_header = bool(NITTER_RETWEET_HEADER_RE.search(item_html))
        wrong_username = item_username.lower() != target_user.lower()

        content = ""
        content_match = NITTER_CONTENT_RE.search(item_html)
        if content_match:
            content = _strip_tags(content_match.group(1))

        looks_like_retweet_text = bool(RT_PREFIX_RE.match(content))

        is_retweet = has_retweet_header or wrong_username or looks_like_retweet_text
        if is_retweet:
            continue

        link_match = NITTER_LINK_RE.search(item_html)
        if not link_match:
            continue
        tweet_id = link_match.group(1)

        date_match = NITTER_DATE_TITLE_RE.search(item_html)
        posted_datetime = _parse_nitter_absolute_timestamp(date_match.group(1)) if date_match else None
        if posted_datetime is None:
            continue  # cannot trust an unparseable timestamp for a direct post

        posts.append({
            "id": tweet_id,
            "content": content,
            "type": "direct",
            "posted_datetime": posted_datetime,
            "timestamp_confidence": "exact",
        })
    return posts


def parse_nitter_direct(html_text):
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    if "timeline-item" not in html_text:
        return [], False
    return extract_direct_posts_nitter(html_text), True


# ---------------------------------------------------------------------------
# Muskmeter parser (retweets only)
# ---------------------------------------------------------------------------

MUSKMETER_TWEET_RE = re.compile(
    r'<a href="https://x\.com/elonmusk/status/(\d+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
MUSKMETER_RT_CONTENT_RE = re.compile(r'<p class="rt-text[^"]*">(.*?)</p>', re.DOTALL)
MUSKMETER_DATE_RE = re.compile(r'<span class="date[^"]*">(.*?)</span>', re.DOTALL)
MUSKMETER_POST_COUNT_RE = re.compile(r'Posts 24h \((\d+)\)')


def parse_muskmeter_relative_time(relative_time, now_utc):
    """Returns (bucket_start_utc, bucket_end_utc) for the whole-minute
    bucket this relative time falls into, or None if unparseable."""
    if not relative_time:
        return None
    try:
        if relative_time in ("now", "Just now"):
            approx = now_utc
        elif "d" in relative_time:
            approx = now_utc - timedelta(days=int(re.search(r'(\d+)d', relative_time).group(1)))
        elif "h" in relative_time:
            approx = now_utc - timedelta(hours=int(re.search(r'(\d+)h', relative_time).group(1)))
        elif "m" in relative_time:
            approx = now_utc - timedelta(minutes=int(re.search(r'(\d+)m', relative_time).group(1)))
        else:
            return None
    except (AttributeError, ValueError):
        return None

    bucket_start = approx.replace(second=0, microsecond=0)
    return bucket_start, bucket_start + timedelta(minutes=1)


def extract_retweets_muskmeter(html_content, now_utc):
    """Returns retweet-only dicts: id, content, bucket_start, page_order
    (0 = most recent, i.e. topmost on page). Direct posts (no rt-text
    block) are skipped -- Nitter owns those.

    Note: Muskmeter has been observed to occasionally emit two distinct
    anchor elements (and therefore two distinct tweet_id values under
    this regex) for what is the same underlying retweet event. That
    cannot be fixed at parse time, since both anchors are syntactically
    valid and there is no reliable way to know from the HTML alone which
    ID (if either) is "canonical." It is instead handled downstream in
    process_fetch_result via a content+timestamp dedup pass -- see
    RETWEET_DUP_WINDOW_SECONDS and its usage below.
    """
    retweets = []
    for page_order, match in enumerate(MUSKMETER_TWEET_RE.finditer(html_content)):
        tweet_id = match.group(1)
        tweet_html = match.group(2)

        rt_match = MUSKMETER_RT_CONTENT_RE.search(tweet_html)
        if not rt_match:
            continue

        content = re.sub(r'<[^>]+>', '', rt_match.group(1)).strip()
        content = re.sub(r'\s+', ' ', content).strip()

        date_match = MUSKMETER_DATE_RE.search(tweet_html)
        relative_time = date_match.group(1).strip() if date_match else ""
        bucket = parse_muskmeter_relative_time(relative_time, now_utc)
        if bucket is None:
            continue

        retweets.append({
            "id": tweet_id,
            "content": content,
            "bucket_start": bucket[0],
            "page_order": page_order,
        })
    return retweets


def assign_even_spacing(retweets_in_same_bucket):
    """offset(i, N) = i * 60 / (N + 1), i = 1..N; i=1 = oldest of the batch
    (earliest offset), i=N = most recent (latest offset). page_order=0 is
    most recent (top of page) so we assign in reverse of page_order."""
    n = len(retweets_in_same_bucket)
    ordered = sorted(retweets_in_same_bucket, key=lambda r: -r["page_order"])
    for i, rt in enumerate(ordered, start=1):
        offset_seconds = (i * 60.0) / (n + 1)
        rt["posted_datetime"] = rt["bucket_start"] + timedelta(seconds=offset_seconds)
        rt["type"] = "retweet"
        rt["timestamp_confidence"] = "approx_minute_bucket"
    return ordered


def process_muskmeter_retweets(html_text, now_utc):
    retweets = extract_retweets_muskmeter(html_text, now_utc)
    buckets = {}
    for rt in retweets:
        key = rt["bucket_start"].isoformat()
        buckets.setdefault(key, []).append(rt)

    result = []
    for group in buckets.values():
        result.extend(assign_even_spacing(group))
    return result


def parse_muskmeter_retweets(html_text, now_utc):
    lower = html_text.lower()
    if is_challenge_page(lower):
        return [], False
    if MUSKMETER_POST_COUNT_RE.search(html_text) is None:
        return [], False
    return process_muskmeter_retweets(html_text, now_utc), True


# ---------------------------------------------------------------------------
# Content-level dedup for retweets (see fix note #3 in module docstring)
# ---------------------------------------------------------------------------

def _normalize_content_for_dedup(content):
    return re.sub(r'\s+', ' ', (content or "")).strip().lower()


def build_existing_retweet_index(existing_rows, header_index):
    """Scans the existing CSV rows once and returns a list of
    (normalized_content, posted_at_utc) tuples for rows that look like
    retweets (content starting with 'RT @', matching the convention used
    elsewhere in this codebase for retweet text). Rows whose posted-at
    field cannot be parsed are skipped (cannot be time-compared, so they
    cannot participate in the dedup window check; this is intentionally
    conservative -- it only ever suppresses items it can positively
    confirm are duplicates, never suppresses on missing data)."""
    content_col = header_index.get("Content")
    posted_col = header_index.get("Posted At (EST)")
    if content_col is None or posted_col is None:
        return []

    index = []
    for row in existing_rows:
        if len(row) <= max(content_col, posted_col):
            continue
        raw_content = row[content_col]
        if not RT_PREFIX_RE.match(raw_content or ""):
            continue
        posted_utc = parse_posted_at_est_to_utc(row[posted_col])
        if posted_utc is None:
            continue
        index.append((_normalize_content_for_dedup(raw_content), posted_utc))
    return index


def find_content_duplicate(candidate_content, candidate_posted_utc, existing_index,
                            window_seconds=RETWEET_DUP_WINDOW_SECONDS):
    """Returns True if candidate matches (same normalized content, posted
    time within window_seconds) an entry already in existing_index."""
    if candidate_posted_utc is None:
        return False
    norm_candidate = _normalize_content_for_dedup(candidate_content)
    if not norm_candidate:
        return False
    for existing_content, existing_posted_utc in existing_index:
        if existing_content != norm_candidate:
            continue
        delta = abs((candidate_posted_utc - existing_posted_utc).total_seconds())
        if delta <= window_seconds:
            return True
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def load_seen_ids():
    header, rows, sha = read_csv_rows()
    log("github.read_csv", "ok", f"rows={len(rows)} sha={sha}")
    return {row[0] for row in rows if row}


def process_fetch_result(source_id, kind, html_text, seen_tweet_ids, cfg):
    now_utc = utc_now()

    if kind == "nitter_direct":
        items, parseable = parse_nitter_direct(html_text)
    elif kind == "muskmeter_retweets":
        items, parseable = parse_muskmeter_retweets(html_text, now_utc)
    else:
        return None

    if not parseable:
        return None

    new_items = [t for t in items if t["id"] not in seen_tweet_ids]
    if not new_items:
        return 0

    try:
        header, existing_rows, _sha = read_csv_rows()
        existing_ids = {row[0] for row in existing_rows if row}
        seen_tweet_ids.update(existing_ids)
    except RuntimeError:
        header, existing_rows = None, []

    new_items = [t for t in new_items if t["id"] not in seen_tweet_ids]
    if not new_items:
        return 0

    # Secondary dedup: content+time match against retweets already on
    # record, independent of the (possibly-duplicated) tweet_id. Only
    # applies to type == "retweet" items, since that is the only path
    # observed to emit two distinct IDs for one real event; direct posts
    # from Nitter carry an exact, source-verified timestamp and ID and
    # are not subject to this check.
    if kind == "muskmeter_retweets" and header is not None:
        header_index = {name: i for i, name in enumerate(header)}
        existing_retweet_index = build_existing_retweet_index(existing_rows, header_index)

        surviving_items = []
        suppressed_count = 0
        for t in new_items:
            if t.get("type") == "retweet" and find_content_duplicate(
                t["content"], t["posted_datetime"], existing_retweet_index
            ):
                suppressed_count += 1
                log(
                    "dedup.content",
                    "duplicate_content_suppressed",
                    f"source={source_id} id={t['id']} "
                    f"posted={t['posted_datetime'].isoformat()} "
                    f"content={t['content'][:80]!r}",
                )
                # Mark as seen so it does not get re-suppressed-then-
                # re-logged every remaining cycle it stays on the page;
                # it will also naturally match on tweet_id from here on
                # once/if it ever legitimately lands in the CSV under
                # this same ID via another path.
                seen_tweet_ids.add(t["id"])
            else:
                surviving_items.append(t)
                # Extend the in-memory index too, so that if muskmeter's
                # own page contains this exact duplicate twice within the
                # *same* fetch (both as "new" relative to the CSV), the
                # second occurrence is caught against the first rather
                # than both slipping through together.
                existing_retweet_index.append(
                    (_normalize_content_for_dedup(t["content"]), t["posted_datetime"])
                )
        new_items = surviving_items
        if suppressed_count:
            log("dedup.content", "summary", f"source={source_id} suppressed={suppressed_count}")

    if not new_items:
        return 0

    for t in new_items:
        seen_tweet_ids.add(t["id"])

    imported_at, imported_date, imported_time = format_datetime_est_from_utc(now_utc)

    rows_to_append = []
    for t in new_items:
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
        for t in new_items:
            seen_tweet_ids.discard(t["id"])
        return None

    if cfg.get("notify_every_post", True):
        ntfy_topic = cfg.get("ntfy_topic", "chan6667")
        for t in new_items[:2]:
            msg = f"{t['type']} post (via {source_id})"
            if t["content"]:
                msg += f"\n{t['content'][:200]}"
            send_ntfy_notification(ntfy_topic, msg, "Elon Musk post", priority=3)

    return len(new_items)


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

                # Emitted before the fetch so a source that hangs or dies
                # mid-fetch still leaves a trace: if "start" appears with
                # no matching "ok"/"error"/"rate_limited"/"unparseable"
                # line before the next cycle's "start" for the same
                # source, that source's fetch/parse step raised something
                # not caught below (or is still blocked).
                log("source.fetch", "start", source["id"])

                try:
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

                except Exception as e:
                    # Broad catch by design: a single source's unexpected
                    # failure (e.g. a bare TimeoutError not wrapped in
                    # URLError, or any other exception the fetch/parse
                    # path did not anticipate) must not silently kill the
                    # collector thread and take the other two sources
                    # down with it for the rest of the process lifetime.
                    log("source.fetch", "unexpected_error", f"{source['id']} {e!r}")

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
