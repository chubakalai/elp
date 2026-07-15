#!/usr/bin/env python3
"""
Flask web interface: displays the latest cluster_analysis.svg inline
(auto-refreshing), and lets the user edit config.json via a simple form
(text inputs, checkboxes, a save button). No JS framework -- plain HTML
form POST + a small polling script to refresh the SVG without a full
page reload.
"""

import os
from flask import Flask, render_template, request, redirect, url_for, Response

from config_store import load_config, save_config

DATA_DIR = os.environ.get("MUSKMETER_DATA_DIR", "/data")
SVG_PATH = os.path.join(DATA_DIR, "cluster_analysis.svg")
RESULTS_CSV = os.path.join(DATA_DIR, "cluster_results.csv")

app = Flask(__name__)

BOOL_FIELDS = ["notify_every_post", "notify_bucket_change", "notify_on_start_stop"]
TEXT_FIELDS = [
    ("window_start", str),
    ("window_end", str),
    ("bucket_width", int),
    ("num_sim_runs", int),
    ("simulated_posts", int),
    ("cluster_gap_minutes", float),
    ("recency_exponent", float),
    ("poll_min_interval", int),
    ("poll_max_interval", int),
    ("poll_start_interval", int),
    ("ntfy_topic", str),
    ("sim_interval_seconds", int),
]


@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    svg_available = os.path.exists(SVG_PATH)
    svg_mtime = None
    if svg_available:
        svg_mtime = os.path.getmtime(SVG_PATH)
    return render_template(
        "index.html",
        cfg=cfg,
        svg_available=svg_available,
        svg_mtime=svg_mtime,
    )


@app.route("/svg", methods=["GET"])
def svg():
    """Serves the raw SVG file, used by the <img>/<object> refresh in the
    template (cache-busted via a query param on the client side)."""
    if not os.path.exists(SVG_PATH):
        return Response("<svg xmlns='http://www.w3.org/2000/svg' width='400' height='60'>"
                         "<text x='10' y='30'>No SVG generated yet</text></svg>",
                         mimetype="image/svg+xml")
    with open(SVG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content, mimetype="image/svg+xml")


@app.route("/status", methods=["GET"])
def status():
    """Small JSON endpoint the page polls to know if the SVG has been
    regenerated, so it only reloads the image when there's something new."""
    mtime = os.path.getmtime(SVG_PATH) if os.path.exists(SVG_PATH) else 0
    return {"svg_mtime": mtime}


@app.route("/config", methods=["POST"])
def update_config():
    updates = {}
    for field in BOOL_FIELDS:
        updates[field] = (request.form.get(field) == "on")
    for field, cast in TEXT_FIELDS:
        raw = request.form.get(field, "").strip()
        if raw == "":
            continue
        try:
            updates[field] = cast(raw)
        except ValueError:
            pass  # ignore unparsable values, keep previous config for that field
    save_config(updates)
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
