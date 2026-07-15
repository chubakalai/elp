#!/usr/bin/env python3
"""
Shared GitHub Contents API helper for reading/writing the CSV file that
lives in the project repo (main branch), plus a small helper for creating
the deployment branch.

Auth: reads the token from the GITHUB_TOKEN environment variable. Never
hardcode a token in this file or in config.json.

Repo layout assumed:
    https://github.com/<GITHUB_REPO> on branch <GITHUB_BRANCH> (default "main")
    CSV lives at <GITHUB_CSV_PATH> (default "data/combined_tweets.csv")
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error

GITHUB_API = "https://api.github.com"

GITHUB_REPO = os.environ.get("GITHUB_REPO", "chubakalai/elp")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_CSV_PATH = os.environ.get("GITHUB_CSV_PATH", "data/combined_tweets.csv")

CSV_HEADER = [
    "Tweet ID", "User", "Content", "Posted At (EST)",
    "Posted Date", "Posted Time", "Imported At (EST)",
    "Imported Date", "Imported Time",
]


def _token():
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise RuntimeError(
            "GITHUB_TOKEN environment variable is not set. "
            "Set it as a Fly.io secret: fly secrets set GITHUB_TOKEN=..."
        )
    return tok


def _request(method, url, data=None, extra_headers=None, timeout=20):
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "muskmeter-app",
    }
    if extra_headers:
        headers.update(extra_headers)

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            return status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": raw.decode("utf-8", errors="replace")}
        return e.code, payload


def get_file(path=None, branch=None, retries=3, backoff=2.0):
    """Fetch a file's content and sha from the repo. Returns (content_str, sha)
    or (None, None) if the file does not exist (404)."""
    path = path or GITHUB_CSV_PATH
    branch = branch or GITHUB_BRANCH
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}?ref={branch}"

    last_err = None
    for attempt in range(retries):
        status, payload = _request("GET", url)
        if status == 200:
            content_b64 = payload.get("content", "")
            sha = payload.get("sha")
            content = base64.b64decode(content_b64.encode("ascii")).decode("utf-8")
            return content, sha
        if status == 404:
            return None, None
        last_err = payload.get("message", f"HTTP {status}")
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GitHub get_file failed for {path}: {last_err}")


def put_file(content_str, message, path=None, branch=None, sha=None, retries=3, backoff=2.0):
    """Create or update a file. If sha is None, this fetches the current sha
    first (to support safe read-modify-write). Returns the new sha."""
    path = path or GITHUB_CSV_PATH
    branch = branch or GITHUB_BRANCH
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"

    if sha is None:
        _, sha = get_file(path=path, branch=branch)

    encoded = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
    data = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        data["sha"] = sha

    last_err = None
    for attempt in range(retries):
        status, payload = _request("PUT", url, data=data)
        if status in (200, 201):
            return payload.get("content", {}).get("sha")
        if status == 409:
            # sha conflict (someone else wrote in the meantime) -- refetch and retry
            _, fresh_sha = get_file(path=path, branch=branch)
            data["sha"] = fresh_sha
            last_err = "409 conflict, retrying with fresh sha"
            time.sleep(backoff * (attempt + 1))
            continue
        last_err = payload.get("message", f"HTTP {status}")
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GitHub put_file failed for {path}: {last_err}")


def ensure_branch(new_branch, from_branch=None):
    """Create `new_branch` pointing at the current tip of `from_branch`
    (default GITHUB_BRANCH) if it doesn't already exist. Used at deploy time
    to create the fly.io branch. Safe to call repeatedly (no-op if it exists)."""
    from_branch = from_branch or GITHUB_BRANCH

    status, ref_payload = _request(
        "GET", f"{GITHUB_API}/repos/{GITHUB_REPO}/git/ref/heads/{new_branch}"
    )
    if status == 200:
        return ref_payload.get("object", {}).get("sha")

    status, base_ref = _request(
        "GET", f"{GITHUB_API}/repos/{GITHUB_REPO}/git/ref/heads/{from_branch}"
    )
    if status != 200:
        raise RuntimeError(f"Could not read base branch {from_branch}: {base_ref}")
    base_sha = base_ref["object"]["sha"]

    status, created = _request(
        "POST",
        f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs",
        data={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
    )
    if status not in (200, 201):
        raise RuntimeError(f"Could not create branch {new_branch}: {created}")
    return created.get("object", {}).get("sha")


def read_csv_rows():
    """Return (header_list, rows_list_of_lists, sha). Creates the file with
    just the header if it does not exist yet (sha will be None until first
    write). Each row is a list of strings matching CSV_HEADER order."""
    import csv
    import io

    content, sha = get_file()
    if content is None:
        return list(CSV_HEADER), [], None

    reader = csv.reader(io.StringIO(content))
    all_rows = list(reader)
    if not all_rows:
        return list(CSV_HEADER), [], sha
    header = all_rows[0]
    rows = all_rows[1:]
    return header, rows, sha


def append_csv_rows(new_rows, commit_message=None):
    """Read current CSV from GitHub, append new_rows (list of lists), and
    write back. Returns the new sha. Safe against concurrent writers via the
    409-retry logic in put_file, but this read-then-write is not atomic
    across processes, so only one collector should run at a time."""
    import csv
    import io

    if not new_rows:
        return None

    header, rows, sha = read_csv_rows()
    if header != CSV_HEADER:
        header = list(CSV_HEADER)

    rows.extend(new_rows)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)

    message = commit_message or f"Append {len(new_rows)} row(s)"
    new_sha = put_file(buf.getvalue(), message, sha=sha)
    return new_sha


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        header, rows, sha = read_csv_rows()
        print(f"Repo: {GITHUB_REPO}  Branch: {GITHUB_BRANCH}  Path: {GITHUB_CSV_PATH}")
        print(f"Header: {header}")
        print(f"Rows: {len(rows)}")
        print(f"sha: {sha}")
    else:
        print("Usage: python github_store.py check")
