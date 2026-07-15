#!/usr/bin/env python3
"""
Simulator: reads the CSV from GitHub, builds recency-weighted clusters,
runs the hazard-based simulation NUM_SIM_RUNS times, writes cluster_results.csv
and cluster_analysis.svg locally (read by web.py), and sends notifications
per config.json.

Runs once per invocation; start.py calls this in a loop every
config['sim_interval_seconds'] (default 60s). All tunables come from
config.json / CLI args -- nothing is hardcoded.

Pure stdlib only.
"""

import argparse
import bisect
import csv
import io
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config_store import load_config
from github_store import read_csv_rows

EASTERN = ZoneInfo("America/New_York")

DATA_DIR = os.environ.get("MUSKMETER_DATA_DIR", "/data")
SVG_PATH = os.path.join(DATA_DIR, "cluster_analysis.svg")
RESULTS_CSV = os.path.join(DATA_DIR, "cluster_results.csv")
BUCKET_STATE_PATH = os.path.join(DATA_DIR, "last_winning_bucket.txt")

RECENCY_EPSILON_MINUTES = 1.0

CURRENT_TIME_UTC = None  # set in main()


def parse_posted_time_to_utc(est_string):
    """Parse 'M/D/YYYY, H:MM:SS AM/PM' as US-Eastern local time (handles
    EST/EDT correctly) and return an aware UTC datetime."""
    naive = datetime.strptime(est_string.strip(), "%m/%d/%Y, %I:%M:%S %p")
    aware_eastern = naive.replace(tzinfo=EASTERN)
    return aware_eastern.astimezone(timezone.utc)


def load_tweets_from_github():
    header, rows, sha = read_csv_rows()
    idx = {name: i for i, name in enumerate(header)}
    posted_col = idx.get("Posted At (EST)")
    times = []
    if posted_col is None:
        return times
    for row in rows:
        if len(row) <= posted_col:
            continue
        posted_est = row[posted_col].strip()
        if not posted_est:
            continue
        try:
            times.append(parse_posted_time_to_utc(posted_est))
        except ValueError:
            continue
    times.sort()
    return times


def get_current_utc_time():
    return datetime.now(timezone.utc)


def recency_weight(event_time, exponent):
    delta_minutes = (CURRENT_TIME_UTC - event_time).total_seconds() / 60.0
    delta_minutes = max(delta_minutes, RECENCY_EPSILON_MINUTES)
    return delta_minutes ** exponent


class Cluster:
    __slots__ = ("index", "posts", "start", "end", "is_odd")

    def __init__(self, index, posts):
        self.index = index
        self.posts = posts
        self.start = posts[0]
        self.end = posts[-1]
        self.is_odd = (index % 2 == 1)


def build_clusters(times, gap_minutes):
    if not times:
        return []
    clusters = []
    current = [times[0]]
    for prev, cur in zip(times, times[1:]):
        gap = (cur - prev).total_seconds() / 60.0
        if gap >= gap_minutes:
            clusters.append(current)
            current = [cur]
        else:
            current.append(cur)
    clusters.append(current)
    return [Cluster(i + 1, posts) for i, posts in enumerate(clusters)]


def within_cluster_minutes_since_start(clusters, exponent):
    out = []
    for c in clusters:
        for p in c.posts:
            val = (p - c.start).total_seconds() / 60.0
            out.append((val, recency_weight(p, exponent)))
    return out


def within_cluster_gaps(clusters, exponent):
    out = []
    for c in clusters:
        for a, b in zip(c.posts, c.posts[1:]):
            gap = (b - a).total_seconds() / 60.0
            out.append((gap, recency_weight(b, exponent)))
    return out


def inter_cluster_gaps(clusters, exponent):
    out = []
    for a, b in zip(clusters, clusters[1:]):
        gap = (b.start - a.end).total_seconds() / 60.0
        out.append((gap, recency_weight(b.start, exponent)))
    return out


def hourly_totals(times):
    totals = [0] * 24
    for t in times:
        totals[t.astimezone(EASTERN).hour] += 1
    return totals


def hourly_probabilities(times):
    totals = hourly_totals(times)
    total_posts = sum(totals)
    if total_posts == 0:
        return [1 / 24] * 24
    return [t / total_posts for t in totals]


def count_posts_in_window(times, start, end):
    return sum(1 for t in times if start <= t <= end)


def bucket_weighted(pairs, bin_width):
    if not pairs:
        return [], []
    hi = max(v for v, _ in pairs)
    n_bins = int(math.floor(hi / bin_width)) + 1
    sums = [0.0] * n_bins
    for v, w in pairs:
        idx = int(math.floor(v / bin_width))
        idx = max(0, min(idx, n_bins - 1))
        sums[idx] += w
    centers = [i * bin_width + bin_width / 2.0 for i in range(n_bins)]
    return centers, sums


def bucket_4h(times):
    labels = ["0-4", "4-8", "8-12", "12-16", "16-20", "20-24"]
    counts = [0] * 6
    for t in times:
        idx = min(t.astimezone(EASTERN).hour // 4, 5)
        counts[idx] += 1
    return labels, counts


def bucket_daily_int_counts(values, bucket_width):
    if not values:
        return [], []
    max_val = max(values)
    num_buckets = (max_val // bucket_width) + 1
    counts = [0] * num_buckets
    for v in values:
        idx = v // bucket_width
        counts[idx] += 1
    labels = []
    for i in range(num_buckets):
        lo = i * bucket_width
        hi = lo + bucket_width - 1
        labels.append(f"{lo}-{hi}")
    return labels, counts


def build_hazard_table(clusters, exponent, bin_width=5.0):
    last_pairs = []
    cont_pairs = []
    for c in clusters:
        for i, p in enumerate(c.posts):
            t_since_start = (p - c.start).total_seconds() / 60.0
            w = recency_weight(p, exponent)
            if i == len(c.posts) - 1:
                last_pairs.append((t_since_start, w))
            else:
                cont_pairs.append((t_since_start, w))

    all_vals = [v for v, _ in last_pairs] + [v for v, _ in cont_pairs]
    max_t = max(all_vals) if all_vals else 45.0
    n_bins = int(math.floor(max_t / bin_width)) + 1

    last_sums = [0.0] * n_bins
    cont_sums = [0.0] * n_bins
    for v, w in last_pairs:
        idx = min(int(math.floor(v / bin_width)), n_bins - 1)
        last_sums[idx] += w
    for v, w in cont_pairs:
        idx = min(int(math.floor(v / bin_width)), n_bins - 1)
        cont_sums[idx] += w

    hazard = []
    for ls, cs in zip(last_sums, cont_sums):
        denom = ls + cs
        hazard.append(ls / denom if denom > 0 else 0.0)

    return bin_width, hazard, last_sums, cont_sums


def hazard_at(bin_width, hazard, t):
    idx = int(math.floor(t / bin_width))
    if idx < 0:
        idx = 0
    if idx >= len(hazard):
        return 1.0
    return hazard[idx]


# ---------------------------------------------------------------------------
# Fast weighted sampling pool
#
# Precomputes, once per (gap pool, hourly rate table), a per-arrival-hour
# cumulative-weight array. Sampling then costs one random() call + one
# bisect (O(log n)) instead of rebuilding an O(n) weights list every draw.
# This is what gets 200 runs x 528 draws well under a minute.
# ---------------------------------------------------------------------------

class FastGapSampler:
    def __init__(self, pairs, hourly_rate_mult):
        # pairs: list of (gap_minutes, recency_weight)
        self.values = [v for v, _ in pairs]
        base_weights = [w for _, w in pairs]
        self.hourly_rate_mult = hourly_rate_mult
        # Precompute cumulative weights for each of the 24 "current hour"
        # buckets. arrival_hour depends on (current_minute_of_day + gap),
        # which varies per draw, but we approximate by bucketing on the
        # CURRENT hour and using its rate multiplier as a stand-in weight
        # correction -- consistent with treating rate as roughly constant
        # over the span of a single gap (gaps are minutes, hours are 60min).
        self.cum_by_hour = []
        for hour in range(24):
            weights = [bw * hourly_rate_mult[hour] for bw in base_weights]
            cum = []
            running = 0.0
            for w in weights:
                running += w
                cum.append(running)
            self.cum_by_hour.append(cum)

    def sample(self, current_hour):
        cum = self.cum_by_hour[current_hour]
        total = cum[-1] if cum else 0.0
        if total <= 0.0:
            return random.choice(self.values) if self.values else None
        r = random.random() * total
        idx = bisect.bisect_left(cum, r)
        if idx >= len(self.values):
            idx = len(self.values) - 1
        return self.values[idx]


def simulate_run(within_sampler, inter_sampler, hazard_bin_width, hazard,
                  start_time, time_since_cluster_start, starting_count,
                  num_posts, seed):
    rng = random.Random(seed)
    current_time = start_time
    t_since_start = time_since_cluster_start
    sim_times = []

    for _ in range(num_posts):
        p_break = hazard_at(hazard_bin_width, hazard, t_since_start)
        is_break = rng.random() < p_break

        sampler = inter_sampler if is_break else within_sampler
        if sampler is None or not sampler.values:
            sampler = inter_sampler if (inter_sampler and inter_sampler.values) else within_sampler
        if sampler is None or not sampler.values:
            break

        current_hour = current_time.astimezone(EASTERN).hour
        # temporarily borrow module-level random via rng for reproducibility
        cum = sampler.cum_by_hour[current_hour]
        total = cum[-1] if cum else 0.0
        if total <= 0.0:
            gap = rng.choice(sampler.values)
        else:
            r = rng.random() * total
            idx = bisect.bisect_left(cum, r)
            if idx >= len(sampler.values):
                idx = len(sampler.values) - 1
            gap = sampler.values[idx]

        current_time = current_time + timedelta(minutes=gap)
        sim_times.append(current_time)

        if is_break:
            t_since_start = 0.0
        else:
            t_since_start += gap

    counts = [starting_count + i for i in range(1, len(sim_times) + 1)]
    return sim_times, counts


def run_multiple_simulations(within_pairs, inter_pairs, hazard_bin_width, hazard,
                              hourly_probs, actual_times, start_time,
                              time_since_cluster_start, final_actual_count,
                              num_runs, simulated_posts, marker_start, marker_end,
                              progress=True):
    rate_mult = [24 * p for p in hourly_probs]
    within_sampler = FastGapSampler(within_pairs, rate_mult) if within_pairs else None
    inter_sampler = FastGapSampler(inter_pairs, rate_mult) if inter_pairs else None

    actual_window = count_posts_in_window(actual_times, marker_start, marker_end)
    total_counts = []

    for run in range(num_runs):
        seed = 42 + run * 100
        sim_times, _ = simulate_run(
            within_sampler, inter_sampler, hazard_bin_width, hazard,
            start_time, time_since_cluster_start, final_actual_count,
            num_posts=simulated_posts, seed=seed,
        )
        sim_window = count_posts_in_window(sim_times, marker_start, marker_end)
        total_counts.append(actual_window + sim_window)

        if progress:
            pct = (run + 1) / num_runs * 100
            bar_len = 40
            filled = int(bar_len * (run + 1) / num_runs)
            bar = "█" * filled + "░" * (bar_len - filled)
            sys.stdout.write(f"\r  Simulating: [{bar}] {run+1}/{num_runs} ({pct:.1f}%)")
            sys.stdout.flush()

    if progress:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return total_counts, actual_window
# ---------------------------------------------------------------------------
# Results CSV + winning bucket / notifications
# ---------------------------------------------------------------------------

def save_results_csv(labels, counts, probabilities, total_runs, actual_window,
                      bucket_width, output_path):
    parent = os.path.dirname(output_path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Bucket", "Count", "Probability", "Cumulative_Probability"])
        cumulative = 0.0
        for label, count, prob in zip(labels, counts, probabilities):
            cumulative += prob
            writer.writerow([label, count, f"{prob:.6f}", f"{cumulative:.6f}"])
        writer.writerow([])
        writer.writerow(["Summary", "", "", ""])
        writer.writerow(["Total runs", total_runs, "", ""])
        writer.writerow(["Actual posts in window", actual_window, "", ""])
        writer.writerow(["Total buckets", len(labels), "", ""])
        writer.writerow(["Bucket width", f"{bucket_width} posts", "", ""])


def read_winning_bucket(path):
    if not os.path.exists(path):
        return None
    best_bucket = None
    best_prob = -1.0
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 3:
                continue
            bucket, _count_str, prob_str = row[0], row[1], row[2]
            if bucket in ("Bucket", "Summary", "") or not bucket:
                continue
            try:
                prob = float(prob_str)
            except ValueError:
                continue
            if prob > best_prob:
                best_prob = prob
                best_bucket = bucket
    return best_bucket


def send_ntfy_notification(topic, message, title, priority=3):
    if not topic:
        return
    import urllib.request
    import urllib.error
    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": str(priority), "Tags": "bar_chart"},
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


def check_and_notify_bucket_change(new_bucket, cfg):
    if new_bucket is None:
        return
    prev_bucket = None
    if os.path.exists(BUCKET_STATE_PATH):
        try:
            with open(BUCKET_STATE_PATH, "r", encoding="utf-8") as f:
                prev_bucket = f.read().strip() or None
        except OSError:
            prev_bucket = None

    if cfg.get("notify_bucket_change", True) and prev_bucket is not None and prev_bucket != new_bucket:
        send_ntfy_notification(
            cfg.get("ntfy_topic", "chan6667"),
            f"Winning bucket changed: {prev_bucket} -> {new_bucket}",
            "Bucket Changed!",
            priority=4,
        )
        print(f"  winning bucket changed: {prev_bucket} -> {new_bucket}")
    elif prev_bucket != new_bucket:
        print(f"  winning bucket: {prev_bucket} -> {new_bucket} (notify disabled or first run)")

    parent = os.path.dirname(BUCKET_STATE_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(BUCKET_STATE_PATH, "w", encoding="utf-8") as f:
        f.write(new_bucket)


# ---------------------------------------------------------------------------
# SVG rendering (structure preserved from original; parameters now injected)
# ---------------------------------------------------------------------------

def svg_axes(pad_left, plot_h, plot_w, y_max_label, y0_label="0"):
    out = []
    out.append(f'<line x1="{pad_left}" y1="{plot_h}" x2="{pad_left+plot_w}" y2="{plot_h}" stroke="black" stroke-width="1"/>')
    out.append(f'<line x1="{pad_left}" y1="0" x2="{pad_left}" y2="{plot_h}" stroke="black" stroke-width="1"/>')
    out.append(f'<text x="{pad_left-5}" y="{plot_h}" text-anchor="end" font-size="10">{y0_label}</text>')
    out.append(f'<text x="{pad_left-5}" y="10" text-anchor="end" font-size="10">{y_max_label}</text>')
    return out


def svg_panel1_clusters(x, y, w, h, clusters, title):
    out = []
    out.append(f'<g transform="translate({x},{y})">')
    out.append(f'<text x="{w/2}" y="-8" text-anchor="middle" font-size="14" font-weight="bold">{title}</text>')

    all_posts = [(p, c) for c in clusters for p in c.posts]
    if not all_posts:
        out.append('<text x="10" y="20" font-size="12">No data</text>')
        out.append('</g>')
        return "\n".join(out)

    pad_left = 55
    pad_bottom = 40
    plot_w = w - pad_left - 15
    plot_h = h - pad_bottom - 10

    dates = sorted(set(p.astimezone(EASTERN).date() for p, _ in all_posts))
    date_to_idx = {d: i for i, d in enumerate(dates)}
    n_days = len(dates)
    day_w = plot_w / n_days

    out.extend(svg_axes(pad_left, plot_h, plot_w, "24:00"))

    for hr in range(0, 25, 4):
        ty = plot_h - (hr * 60 / 1440.0) * plot_h
        out.append(f'<text x="{pad_left-5}" y="{ty+3:.1f}" text-anchor="end" font-size="9">{hr:02d}:00</text>')
        out.append(f'<line x1="{pad_left}" y1="{ty:.1f}" x2="{pad_left+plot_w}" y2="{ty:.1f}" stroke="#ddd" stroke-width="0.5"/>')

    label_stride = max(1, n_days // 15)
    for i, d in enumerate(dates):
        if i % label_stride != 0:
            continue
        lx = pad_left + i * day_w + day_w / 2
        out.append(f'<text x="{lx:.1f}" y="{plot_h+15}" text-anchor="end" font-size="8" transform="rotate(-45 {lx:.1f},{plot_h+15})">{d.strftime("%m/%d")}</text>')

    minute_h = plot_h / 1440.0
    strip_h = max(minute_h, 0.6)

    for p, c in all_posts:
        p_local = p.astimezone(EASTERN)
        d_idx = date_to_idx[p_local.date()]
        minute_of_day = p_local.hour * 60 + p_local.minute + p_local.second / 60.0
        bx = pad_left + d_idx * day_w
        by = plot_h - (minute_of_day / 1440.0) * plot_h - strip_h
        color = "#999999" if c.is_odd else "#000000"
        out.append(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{day_w:.2f}" height="{strip_h:.2f}" fill="{color}" fill-opacity="0.85"/>')

    out.append(f'<text x="{pad_left+plot_w-5}" y="15" text-anchor="end" font-size="11" fill="#333">{len(clusters)} clusters, {len(all_posts)} posts (gray=odd, black=even)</text>')
    out.append('</g>')
    return "\n".join(out)


def svg_histogram(x, y, w, h, centers, values, title, x_label, extra_text=None,
                   bar_color="#4C72B0", label_fmt="{:.0f}", tick_labels=None,
                   y_label_fmt="{:.0f}"):
    out = []
    out.append(f'<g transform="translate({x},{y})">')
    out.append(f'<text x="{w/2}" y="-8" text-anchor="middle" font-size="14" font-weight="bold">{title}</text>')
    if not values:
        out.append('<text x="10" y="20" font-size="12">No data</text>')
        out.append('</g>')
        return "\n".join(out)

    pad_left = 55
    pad_bottom = 45
    pad_right = 15
    plot_w = w - pad_left - pad_right
    plot_h = h - pad_bottom - 10

    max_val = max(values) if values else 1.0
    max_val = max(max_val, 1e-9)
    n = len(values)
    bar_w = plot_w / n

    out.extend(svg_axes(pad_left, plot_h, plot_w, y_label_fmt.format(max_val)))

    for i, v in enumerate(values):
        bh = (v / max_val) * plot_h
        bx = pad_left + i * bar_w
        by = plot_h - bh
        out.append(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{max(bar_w*0.9,0.5):.2f}" height="{bh:.2f}" fill="{bar_color}" stroke="white" stroke-width="0.3"/>')

    label_stride = max(1, n // 15)
    labels_to_use = tick_labels if tick_labels is not None else [label_fmt.format(c) for c in centers]
    for i, lab in enumerate(labels_to_use):
        if i % label_stride != 0:
            continue
        lx = pad_left + i * bar_w + bar_w / 2
        out.append(f'<text x="{lx:.2f}" y="{plot_h+14}" text-anchor="end" font-size="9" transform="rotate(-45 {lx:.2f},{plot_h+14})">{lab}</text>')

    out.append(f'<text x="{pad_left+plot_w/2}" y="{plot_h+40}" text-anchor="middle" font-size="10">{x_label}</text>')

    if extra_text:
        out.append(f'<text x="{pad_left+plot_w-5}" y="15" text-anchor="end" font-size="11" fill="#333">{extra_text}</text>')

    out.append('</g>')
    return "\n".join(out)


def svg_probability_panel(x, y, w, h, labels, counts, probs, title, extra_text=None):
    out = []
    out.append(f'<g transform="translate({x},{y})">')
    out.append(f'<text x="{w/2}" y="-8" text-anchor="middle" font-size="14" font-weight="bold">{title}</text>')
    if not counts:
        out.append('<text x="10" y="20" font-size="12">No data</text>')
        out.append('</g>')
        return "\n".join(out)

    pad_left = 55
    pad_bottom = 45
    pad_right = 15
    plot_w = w - pad_left - pad_right
    plot_h = h - pad_bottom - 10

    max_prob = max(probs) if probs else 1.0
    max_prob = max(max_prob, 1e-9)
    n = len(probs)
    bar_w = plot_w / n

    out.extend(svg_axes(pad_left, plot_h, plot_w, f"{max_prob*100:.0f}%"))

    for i in range(1, 5):
        frac = i / 4
        gy = plot_h - frac * plot_h
        gval = frac * max_prob * 100
        out.append(f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{pad_left+plot_w}" y2="{gy:.1f}" stroke="#eee" stroke-width="0.5"/>')
        out.append(f'<text x="{pad_left-5}" y="{gy+3:.1f}" text-anchor="end" font-size="9">{gval:.0f}%</text>')

    for i, (p, c) in enumerate(zip(probs, counts)):
        bh = (p / max_prob) * plot_h
        bx = pad_left + i * bar_w
        by = plot_h - bh
        out.append(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{max(bar_w*0.9,0.5):.2f}" height="{bh:.2f}" fill="#8172B2" stroke="white" stroke-width="0.3"/>')
        if p > 0:
            out.append(f'<text x="{bx+bar_w*0.45:.2f}" y="{by-3:.1f}" text-anchor="middle" font-size="7.5">{p*100:.1f}%</text>')

    label_stride = max(1, n // 15)
    for i, lab in enumerate(labels):
        if i % label_stride != 0:
            continue
        lx = pad_left + i * bar_w + bar_w / 2
        out.append(f'<text x="{lx:.2f}" y="{plot_h+14}" text-anchor="end" font-size="9" transform="rotate(-45 {lx:.2f},{plot_h+14})">{lab}</text>')

    out.append(f'<text x="{pad_left+plot_w/2}" y="{plot_h+40}" text-anchor="middle" font-size="10">total posts in configured window (bucket)</text>')

    if extra_text:
        out.append(f'<text x="{pad_left+plot_w-5}" y="15" text-anchor="end" font-size="11" fill="#333">{extra_text}</text>')

    out.append('</g>')
    return "\n".join(out)


def svg_cumulative_panel(x, y, w, h, actual_times, actual_counts, sim_times, sim_counts,
                          marker_start, marker_end, actual_window_count, sim_window_count,
                          title, extra_text=None):
    out = []
    out.append(f'<g transform="translate({x},{y})">')
    out.append(f'<text x="{w/2}" y="-8" text-anchor="middle" font-size="14" font-weight="bold">{title}</text>')
    if not actual_times and not sim_times:
        out.append('<text x="10" y="20" font-size="12">No data</text>')
        out.append('</g>')
        return "\n".join(out)

    pad_left = 65
    pad_bottom = 65
    pad_right = 15
    plot_w = w - pad_left - pad_right
    plot_h = h - pad_bottom - 15

    all_times = list(actual_times) + list(sim_times) + [marker_start, marker_end]
    min_time = min(all_times)
    max_time = max(all_times)
    span = (max_time - min_time).total_seconds() or 3600.0

    all_y = (list(actual_counts) + list(sim_counts)) or [1]
    max_y = max(max(all_y), 1)

    def t2x(dt):
        return pad_left + ((dt - min_time).total_seconds() / span) * plot_w

    out.extend(svg_axes(pad_left, plot_h, plot_w, f"{max_y:.0f}"))

    for i in range(9):
        frac = i / 8
        tt = min_time + timedelta(seconds=frac * span)
        lx = pad_left + frac * plot_w
        label = tt.astimezone(EASTERN).strftime("%m/%d")
        out.append(f'<text x="{lx:.1f}" y="{plot_h+15}" text-anchor="middle" font-size="9">{label}</text>')

    if actual_times and sim_times:
        tx = t2x(actual_times[-1])
        out.append(f'<line x1="{tx:.1f}" y1="0" x2="{tx:.1f}" y2="{plot_h}" stroke="#888" stroke-width="0.8" stroke-dasharray="3,5"/>')
        out.append(f'<text x="{tx:.1f}" y="{plot_h+30}" text-anchor="middle" font-size="8" fill="#888">sim start</text>')

    for marker, color, label in [
        (marker_start, "#2ECC40", marker_start.astimezone(EASTERN).strftime("%b %d %H:%M")),
        (marker_end, "#FF4136", marker_end.astimezone(EASTERN).strftime("%b %d %H:%M")),
    ]:
        mx = t2x(marker)
        if pad_left <= mx <= pad_left + plot_w:
            out.append(f'<line x1="{mx:.1f}" y1="0" x2="{mx:.1f}" y2="{plot_h}" stroke="{color}" stroke-width="1.5" stroke-dasharray="6,4"/>')
            out.append(f'<text x="{mx:.1f}" y="-5" text-anchor="middle" font-size="9" fill="{color}" font-weight="bold">{label}</text>')

    total_window = actual_window_count + sim_window_count
    out.append(f'<text x="{pad_left+plot_w/2}" y="{plot_h+55}" text-anchor="middle" font-size="10" fill="#333" font-weight="bold">Actual: {actual_window_count} + Sim: {sim_window_count} = {total_window} posts in window</text>')

    if actual_times:
        pts = " ".join(f"{t2x(t):.2f},{plot_h - (c/max_y)*plot_h:.2f}" for t, c in zip(actual_times, actual_counts))
        out.append(f'<polyline points="{pts}" fill="none" stroke="#4C72B0" stroke-width="2"/>')
    if sim_times:
        pts = " ".join(f"{t2x(t):.2f},{plot_h - (c/max_y)*plot_h:.2f}" for t, c in zip(sim_times, sim_counts))
        out.append(f'<polyline points="{pts}" fill="none" stroke="#E24A33" stroke-width="2" stroke-dasharray="5,3"/>')

    legend_y = 8
    out.append(f'<line x1="{pad_left+plot_w-150}" y1="{legend_y}" x2="{pad_left+plot_w-130}" y2="{legend_y}" stroke="#4C72B0" stroke-width="2"/>')
    out.append(f'<text x="{pad_left+plot_w-125}" y="{legend_y+4}" font-size="10">Actual</text>')
    out.append(f'<line x1="{pad_left+plot_w-70}" y1="{legend_y}" x2="{pad_left+plot_w-50}" y2="{legend_y}" stroke="#E24A33" stroke-width="2" stroke-dasharray="5,3"/>')
    out.append(f'<text x="{pad_left+plot_w-45}" y="{legend_y+4}" font-size="10">Sim</text>')

    if extra_text:
        out.append(f'<text x="{pad_left+plot_w-5}" y="25" text-anchor="end" font-size="11" fill="#333">{extra_text}</text>')

    out.append('</g>')
    return "\n".join(out)
# ---------------------------------------------------------------------------
# Assembly + entrypoint
# ---------------------------------------------------------------------------

def parse_window_datetime(date_str, hour=12, minute=0):
    """Parse 'YYYY-MM-DD' as a US-Eastern local date at the given hour:minute
    (default noon ET) and return an aware UTC datetime."""
    d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    d = d.replace(hour=hour, minute=minute, second=0)
    d_eastern = d.replace(tzinfo=EASTERN)
    return d_eastern.astimezone(timezone.utc)


def build_svg(times, cfg):
    gap_minutes = float(cfg.get("cluster_gap_minutes", 45.0))
    exponent = float(cfg.get("recency_exponent", -1.0 / 13.0))
    bucket_width = int(cfg.get("bucket_width", 20))
    num_sim_runs = int(cfg.get("num_sim_runs", 200))
    simulated_posts = int(cfg.get("simulated_posts", 528))
    marker_start = parse_window_date(cfg.get("window_start", "2026-07-13"), end_of_day=False)
    marker_end = parse_window_date(cfg.get("window_end", "2026-07-15"), end_of_day=True)

    clusters = build_clusters(times, gap_minutes)
    total_tweets = len(times)

    p1_svg = svg_panel1_clusters(30, 40, 900 - 60, 300 - 20, clusters,
                                  "Post Clusters (gray=odd, black=even)")

    within_start_pairs = within_cluster_minutes_since_start(clusters, exponent)
    p2_centers, p2_weighted_sums = bucket_weighted(within_start_pairs, bin_width=2.0)
    p2_extra = f"n={len(within_start_pairs)}, recency-weighted (exp={exponent:.4f})"

    p3_labels, p3_counts = bucket_4h(times)
    p3_centers = list(range(len(p3_labels)))

    ic_gap_pairs = inter_cluster_gaps(clusters, exponent)
    p4_centers, p4_weighted_sums = bucket_weighted(ic_gap_pairs, bin_width=15.0)
    p4_extra = f"n={len(ic_gap_pairs)} cluster transitions, recency-weighted (exp={exponent:.4f})"

    within_gap_pairs = within_cluster_gaps(clusters, exponent)
    hazard_bin_width, hazard, _last_sums, _cont_sums = build_hazard_table(clusters, exponent, bin_width=5.0)
    hourly_probs = hourly_probabilities(times)

    if not clusters:
        raise SystemExit("No clusters could be built -- no data to simulate.")

    last_cluster = clusters[-1]
    sim_start_time = last_cluster.end
    time_since_cluster_start = (last_cluster.end - last_cluster.start).total_seconds() / 60.0
    time_since_last_post = max(0.0, (CURRENT_TIME_UTC - sim_start_time).total_seconds() / 60.0)

    rate_mult = [24 * p for p in hourly_probs]
    within_sampler = FastGapSampler(within_gap_pairs, rate_mult) if within_gap_pairs else None
    inter_sampler = FastGapSampler(ic_gap_pairs, rate_mult) if ic_gap_pairs else None

    sim_times, sim_counts = simulate_run(
        within_sampler, inter_sampler, hazard_bin_width, hazard,
        sim_start_time, time_since_cluster_start, total_tweets,
        num_posts=simulated_posts, seed=42,
    )

    actual_window_count = count_posts_in_window(times, marker_start, marker_end)
    sim_window_count = count_posts_in_window(sim_times, marker_start, marker_end)
    p5_extra = f"Total tweets: {total_tweets} -> +{len(sim_times)} sim | last gap: {time_since_last_post:.0f}m"

    t0 = time.perf_counter()
    print(f"Running {num_sim_runs} simulations...")
    total_counts, actual_window = run_multiple_simulations(
        within_gap_pairs, ic_gap_pairs, hazard_bin_width, hazard,
        hourly_probs, times, sim_start_time, time_since_cluster_start,
        total_tweets, num_runs=num_sim_runs, simulated_posts=simulated_posts,
        marker_start=marker_start, marker_end=marker_end,
    )
    elapsed = time.perf_counter() - t0
    print(f"  {num_sim_runs} runs completed in {elapsed:.2f}s")

    labels5, counts5 = bucket_daily_int_counts(total_counts, bucket_width=bucket_width)
    probs5 = [c / num_sim_runs for c in counts5]
    save_results_csv(labels5, counts5, probs5, num_sim_runs, actual_window, bucket_width, RESULTS_CSV)
    print(f"Saved bucket probabilities to {RESULTS_CSV}")

    winning_bucket = read_winning_bucket(RESULTS_CSV)
    check_and_notify_bucket_change(winning_bucket, cfg)

    total_w = 900
    panel_h = 300
    margin_top = 40
    panel_gap = 30
    num_panels = 6
    svg_h = margin_top + num_panels * panel_h + (num_panels - 1) * panel_gap + 20

    parts = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{svg_h}" font-family="Helvetica,Arial,sans-serif">')
    parts.append(f'<rect x="0" y="0" width="{total_w}" height="{svg_h}" fill="white"/>')

    parts.append(p1_svg)

    y2 = margin_top + panel_h + panel_gap
    parts.append(svg_histogram(30, y2, total_w - 60, panel_h - 20, p2_centers, p2_weighted_sums,
                                "Within-Cluster Timing (recency-weighted sum, minutes since cluster start)",
                                "minutes since cluster start", p2_extra, "#55A868", "{:.0f}",
                                y_label_fmt="{:.2f}"))

    y3 = y2 + panel_h + panel_gap
    parts.append(svg_histogram(30, y3, total_w - 60, panel_h - 20, p3_centers, p3_counts,
                                "Posts by Hour of Day (4h bins, Eastern)", "time of day",
                                None, "#4C72B0", "{}", tick_labels=p3_labels))

    y4 = y3 + panel_h + panel_gap
    parts.append(svg_histogram(30, y4, total_w - 60, panel_h - 20, p4_centers, p4_weighted_sums,
                                "Inter-Cluster Gaps (recency-weighted sum, minutes since previous cluster end)",
                                "minutes since previous cluster end", p4_extra, "#FF7F0E", "{:.0f}",
                                y_label_fmt="{:.2f}"))

    y5 = y4 + panel_h + panel_gap
    parts.append(svg_cumulative_panel(30, y5, total_w - 60, panel_h - 20,
                                       times, list(range(1, total_tweets + 1)),
                                       sim_times, sim_counts,
                                       marker_start, marker_end,
                                       actual_window_count, sim_window_count,
                                       f"Cumulative Posts vs Time ({len(sim_times)} Simulated)",
                                       p5_extra))

    y6 = y5 + panel_h + panel_gap
    p6_extra = f"n_runs={num_sim_runs}, bucket width={bucket_width}, sim_time={elapsed:.1f}s"
    parts.append(svg_probability_panel(30, y6, total_w - 60, panel_h - 20,
                                        labels5, counts5, probs5,
                                        "Bucket Probabilities: Total Posts in Configured Window",
                                        p6_extra))

    parts.append('</svg>')
    return "\n".join(parts)


def run_once(cfg=None):
    """Runs a single collect-simulate-render cycle. Returns True on success."""
    global CURRENT_TIME_UTC
    CURRENT_TIME_UTC = get_current_utc_time()

    cfg = cfg or load_config()

    print(f"[{CURRENT_TIME_UTC.isoformat()}] Loading tweets from GitHub CSV...")
    times = load_tweets_from_github()
    print(f"Loaded {len(times)} tweets")
    if not times:
        print("No tweets loaded -- skipping this cycle.")
        return False
    print(f"Date range: {times[0].isoformat()} to {times[-1].isoformat()}")

    svg = build_svg(times, cfg)

    parent = os.path.dirname(SVG_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    tmp_path = SVG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(svg)
    os.replace(tmp_path, SVG_PATH)

    print(f"Wrote SVG to {SVG_PATH}")
    return True


def run_simulator_loop(stop_event=None):
    """Runs run_once() every cfg['sim_interval_seconds'] (default 60s),
    re-reading config each iteration so web-UI edits take effect on the
    next cycle. Intended to be launched by start.py."""
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        cfg = load_config()
        try:
            run_once(cfg)
        except Exception as e:
            print(f"[simulate] cycle failed: {e}")

        interval = int(cfg.get("sim_interval_seconds", 60))
        # sleep in small increments so stop_event is honored promptly
        slept = 0
        while slept < interval:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(1, interval - slept))
            slept += 1


def main():
    parser = argparse.ArgumentParser(description="MuskMeter cluster simulator")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (default if no --loop)")
    parser.add_argument("--loop", action="store_true", help="Run continuously per config sim_interval_seconds")
    parser.add_argument("--window-start", help="Override window_start (YYYY-MM-DD)")
    parser.add_argument("--window-end", help="Override window_end (YYYY-MM-DD)")
    parser.add_argument("--bucket-width", type=int, help="Override bucket_width")
    parser.add_argument("--num-sim-runs", type=int, help="Override num_sim_runs")
    parser.add_argument("--simulated-posts", type=int, help="Override simulated_posts")
    args = parser.parse_args()

    overrides = {}
    if args.window_start:
        overrides["window_start"] = args.window_start
    if args.window_end:
        overrides["window_end"] = args.window_end
    if args.bucket_width is not None:
        overrides["bucket_width"] = args.bucket_width
    if args.num_sim_runs is not None:
        overrides["num_sim_runs"] = args.num_sim_runs
    if args.simulated_posts is not None:
        overrides["simulated_posts"] = args.simulated_posts

    cfg = load_config()
    cfg.update(overrides)

    if args.loop:
        run_simulator_loop()
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
    
