#!/usr/bin/env python3
"""
Shared configuration load/save helper.

Config lives at a local path (CONFIG_PATH, default /data/config.json on the
Fly.io volume, or ./config.json locally). Every process (collector, simulator,
web) re-reads this file each time it needs config -- no long-lived in-memory
cache -- so that edits made via the web interface take effect on the next
loop iteration without restarting anything.
"""

import json
import os
import threading

DEFAULT_CONFIG_PATH = os.environ.get("MUSKMETER_CONFIG_PATH", "/data/config.json")

DEFAULTS = {
    "notify_every_post": True,
    "notify_bucket_change": True,
    "notify_on_start_stop": True,
    "window_start": "2026-07-13",
    "window_end": "2026-07-15",
    "bucket_width": 20,
    "num_sim_runs": 200,
    "simulated_posts": 528,
    "cluster_gap_minutes": 45.0,
    "recency_exponent": -0.0769230769,
    "poll_min_interval": 3,
    "poll_max_interval": 60,
    "poll_start_interval": 5,
    "ntfy_topic": "chan6667",
    "sim_interval_seconds": 60,
}

_lock = threading.Lock()


def _ensure_parent_dir(path):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def load_config(path=None):
    """Read config from disk, filling in any missing keys with defaults.
    Never raises: if the file is missing or corrupt, returns DEFAULTS
    (and writes a fresh file so subsequent reads/edits have something to work
    with)."""
    path = path or DEFAULT_CONFIG_PATH
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
                if isinstance(on_disk, dict):
                    cfg.update(on_disk)
            except (json.JSONDecodeError, OSError):
                pass
        else:
            _ensure_parent_dir(path)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except OSError:
                pass
        return cfg


def save_config(updates, path=None):
    """Merge `updates` into the existing config on disk and write it back.
    Returns the full merged config."""
    path = path or DEFAULT_CONFIG_PATH
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
                if isinstance(on_disk, dict):
                    cfg.update(on_disk)
            except (json.JSONDecodeError, OSError):
                pass
        cfg.update(updates)
        _ensure_parent_dir(path)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp_path, path)
        return cfg


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        print(json.dumps(load_config(), indent=2))
    else:
        print(f"Config path: {DEFAULT_CONFIG_PATH}")
        print(json.dumps(load_config(), indent=2))
