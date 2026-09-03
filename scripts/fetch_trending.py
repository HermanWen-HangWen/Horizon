#!/usr/bin/env python3
"""Fetch GitHub trending repos with rich data.

Strategy:
1. mshibanami/GitHubTrendingRSS provides the OFFICIAL trending order (it's
   a daily rollup of github.com/trending). Use it to get ranked repo names.
2. GitHub REST API enriches each repo with total_stars, language, description.
3. Events API counts WatchEvents (= stars) in the last 24h for `+N today`.

Output: data/trending.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_PATH = DATA_DIR / "trending.json"

# Load .env before reading GITHUB_TOKEN.
load_dotenv(PROJECT_DIR / ".env")

# RSS feeds (one per "language" view). Order preserved = trending ranking.
RSS_FEEDS = [
    ("all",       "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml"),
    ("python",    "https://mshibanami.github.io/GitHubTrendingRSS/daily/python.xml"),
    ("typescript","https://mshibanami.github.io/GitHubTrendingRSS/daily/typescript.xml"),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--per-language", type=int, default=10, help="Top N per language (default: 10)")
    p.add_argument("--output", default=str(OUTPUT_PATH), help="Output JSON path")
    return p.parse_args()


def _http_err(prefix: str, url: str, exc: Exception) -> str:
    """Format an httpx failure with exception class + HTTP status (if any).

    Examples:
      ❌ RSS all: ConnectError — [Errno 8] nodename nor servname provided (...)
      ❌ stats owner/repo: HTTPStatusError [HTTP 403] — 403 Forbidden
    """
    cls = type(exc).__name__
    code = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = f" [HTTP {resp.status_code}]"
    return f"❌ [Github Trending] {prefix}: {cls}{code} — {exc} | url={url}"


def fetch_rss_repo_names(client: httpx.Client, label: str, rss_url: str) -> list | None:
    """Fetch trending RSS and extract ordered repo names.

    Returns list of {rank, language, full_name, url} preserving RSS order.
    Returns None on network/HTTP failure (so caller can distinguish "fetch
    failed" from "feed legitimately empty").
    """
    try:
        resp = client.get(rss_url, timeout=30.0, headers={"User-Agent": "Horizon/1.0"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(_http_err(f"RSS {label}", rss_url, e), file=sys.stderr)
        return None

    feed = feedparser.parse(resp.text)
    repos = []
    for entry in feed.entries:
        # Title format: "owner/repo" (e.g., "XiaoDuoYa/codex-with-chatgpt")
        full_name = (entry.get("title") or "").strip()
        if not re.match(r"^[^/\s]+/[^/\s]+$", full_name):
            continue  # skip malformed entries
        repos.append({
            "rank": len(repos) + 1,
            "language": label,
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
        })
    return repos


def enrich_repo_stats(client: httpx.Client, headers: dict, repo: dict) -> bool:
    """Fetch total_stars, description, language via /repos/{owner}/{repo}.

    Mutates `repo` in place. Returns True on success, False on HTTP failure.
    """
    url = f"https://api.github.com/repos/{repo['full_name']}"
    try:
        resp = client.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(_http_err(f"stats {repo['full_name']}", url, e), file=sys.stderr)
        return False

    data = resp.json()
    repo["total_stars"] = data.get("stargazers_count", 0)
    repo["description"] = (data.get("description") or "").strip()
    repo["primary_language"] = data.get("language") or "Unknown"
    repo["forks"] = data.get("forks_count", 0)
    repo["created_at"] = data.get("created_at", "")
    repo["pushed_at"] = data.get("pushed_at", "")
    repo["topics"] = data.get("topics", [])[:5]
    return True


def count_stars_today(client: httpx.Client, headers: dict, full_name: str) -> tuple[int, bool]:
    """Count WatchEvents (= stars) in last 24h via the Events API.

    Works with default OAuth scopes (no `public_repo` needed). Capped at
    1 page (100 events) since GitHub serves the 300 most recent events;
    for trending repos this is plenty for an estimate.

    Returns (count, ok) so caller can distinguish "0 stars in 24h" from
    "fetch failed".
    """
    url = f"https://api.github.com/repos/{full_name}/events?per_page=100"
    try:
        resp = client.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(_http_err(f"events {full_name}", url, e), file=sys.stderr)
        return 0, False

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for event in resp.json():
        if event.get("type") != "WatchEvent":
            continue
        try:
            ts = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
            if ts > cutoff:
                count += 1
        except (KeyError, ValueError):
            continue
    return count, True


def main() -> int:
    args = parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ [Github Trending] GITHUB_TOKEN not set in env (.env loaded?).", file=sys.stderr)
        return 1

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Horizon-Trending-Fetcher",
    }

    all_results = []
    rss_failed = []
    with httpx.Client() as client:
        # 1. Pull ranked repo names from mshibanami's RSS (= GitHub Trending order).
        for label, rss_url in RSS_FEEDS:
            print(f"[Github Trending] 📡 RSS {label}…", file=sys.stderr)
            repos = fetch_rss_repo_names(client, label, rss_url)
            if repos is None:
                rss_failed.append(label)
                print(f"[Github Trending]    → 0 repos (RSS fetch failed; see error above)", file=sys.stderr)
                continue
            repos = repos[: args.per_language]
            print(f"[Github Trending]    → {len(repos)} repos", file=sys.stderr)
            all_results.extend(repos)

        # 2. Enrich each repo with /repos/{owner}/{repo} data.
        stats_ok = 0
        if all_results:
            print(f"[Github Trending] 📊 Enriching {len(all_results)} repos…", file=sys.stderr)
            for r in all_results:
                if enrich_repo_stats(client, headers, r):
                    stats_ok += 1

        # 3. Count stars_today per repo.
        events_ok = 0
        if all_results:
            print(f"[Github Trending] ⭐ Counting 24h stars…", file=sys.stderr)
            for r in all_results:
                _, ok = count_stars_today(client, headers, r["full_name"])
                # Always set stars_today so JSON schema stays uniform.
                r.setdefault("stars_today", 0)
                if ok:
                    events_ok += 1
                else:
                    r["stars_today"] = 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "mshibanami/GitHubTrendingRSS (rank) + GitHub REST API (stats) + Events API (24h)",
        "languages": [l[0] for l in RSS_FEEDS],
        "repos": all_results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[Github Trending] 📋 Summary: RSS ok={len(RSS_FEEDS) - len(rss_failed)}/{len(RSS_FEEDS)}"
        f" ({'failed: ' + ', '.join(rss_failed) if rss_failed else 'all good'})"
        f" · stats ok={stats_ok}/{len(all_results)}"
        f" · events ok={events_ok}/{len(all_results)}",
        file=sys.stderr,
    )
    print(f"[Github Trending] ✅ Wrote {len(all_results)} repos to {out_path.relative_to(PROJECT_DIR)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())