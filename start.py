#!/usr/bin/env python3
"""
Starter: single Fly.io entrypoint. Launches the collector (elc8) and the
simulator loop as background threads, and runs the Flask web server in the
foreground bound to $PORT, since Fly.io expects one process listening on
that port.

Threads (not subprocesses) are used deliberately: all three components are
pure-Python, I/O-bound (network calls, file I/O), and share the GIL-friendly
workload of waiting on requests/sleep -- so threads are sufficient and avoid
the overhead/complexity of managing separate OS processes, log multiplexing,
and restart logic that subprocess management would require.
"""

import os
import signal
import sys
import threading

from elc8 import run_collector
from simulate import run_simulator_loop
import web

stop_event = threading.Event()


def _handle_signal(signum, frame):
    print(f"\nReceived signal {signum}, shutting down...")
    stop_event.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    data_dir = os.environ.get("MUSKMETER_DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)

    if not os.environ.get("GITHUB_TOKEN"):
        print("WARNING: GITHUB_TOKEN is not set. Collector and simulator "
              "GitHub reads/writes will fail until it is configured as a "
              "Fly.io secret (fly secrets set GITHUB_TOKEN=...).", file=sys.stderr)

    collector_thread = threading.Thread(
        target=run_collector, args=(stop_event,), name="collector", daemon=True
    )
    simulator_thread = threading.Thread(
        target=run_simulator_loop, args=(stop_event,), name="simulator", daemon=True
    )

    collector_thread.start()
    simulator_thread.start()

    print("Collector and simulator threads started. Starting web server...")

    port = int(os.environ.get("PORT", 8080))
    try:
        # Flask's dev server is fine for a single-container Fly.io app with
        # light traffic; blocks in the foreground until interrupted.
        web.app.run(host="0.0.0.0", port=port, use_reloader=False)
    finally:
        stop_event.set()
        collector_thread.join(timeout=10)
        simulator_thread.join(timeout=10)
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
