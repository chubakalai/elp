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
