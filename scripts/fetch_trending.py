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


def fetch_rss_repo_names(client: httpx.Client, label: str, rss_url: str) -> list:
    """Fetch trending RSS and extract ordered repo names.

    Returns list of {rank, language, full_name, url} preserving RSS order.
    """
    try:
        resp = client.get(rss_url, timeout=30.0, headers={"User-Agent": "Horizon/1.0"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"⚠️  {label}: {e}", file=sys.stderr)
        return []

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


def enrich_repo_stats(client: httpx.Client, headers: dict, repo: dict) -> None:
    """Fetch total_stars, description, language via /repos/{owner}/{repo}."""
    try:
        resp = client.get(
            f"https://api.github.com/repos/{repo['full_name']}",
            headers=headers, timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"   ⚠️  stats for {repo['full_name']}: {e}", file=sys.stderr)
        return

    data = resp.json()
    repo["total_stars"] = data.get("stargazers_count", 0)
    repo["description"] = (data.get("description") or "").strip()
    repo["primary_language"] = data.get("language") or "Unknown"
    repo["forks"] = data.get("forks_count", 0)
    repo["created_at"] = data.get("created_at", "")
    repo["pushed_at"] = data.get("pushed_at", "")
    repo["topics"] = data.get("topics", [])[:5]


def count_stars_today(client: httpx.Client, headers: dict, full_name: str) -> int:
    """Count WatchEvents (= stars) in last 24h via the Events API.

    Works with default OAuth scopes (no `public_repo` needed). Capped at
    1 page (100 events) since GitHub serves the 300 most recent events;
    for trending repos this is plenty for an estimate.
    """
    try:
        resp = client.get(
            f"https://api.github.com/repos/{full_name}/events?per_page=100",
            headers=headers, timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"   ⚠️  events for {full_name}: {e}", file=sys.stderr)
        return 0

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
    return count


def main() -> int:
    args = parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ GITHUB_TOKEN not set in env (.env loaded?).", file=sys.stderr)
        return 1

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Horizon-Trending-Fetcher",
    }

    all_results = []
    with httpx.Client() as client:
        # 1. Pull ranked repo names from mshibanami's RSS (= GitHub Trending order).
        for label, rss_url in RSS_FEEDS:
            print(f"📡 RSS {label}…", file=sys.stderr)
            repos = fetch_rss_repo_names(client, label, rss_url)
            repos = repos[: args.per_language]
            print(f"   → {len(repos)} repos", file=sys.stderr)
            all_results.extend(repos)

        # 2. Enrich each repo with /repos/{owner}/{repo} data.
        if all_results:
            print(f"📊 Enriching {len(all_results)} repos…", file=sys.stderr)
            for r in all_results:
                enrich_repo_stats(client, headers, r)

        # 3. Count stars_today per repo.
        if all_results:
            print(f"⭐ Counting 24h stars…", file=sys.stderr)
            for r in all_results:
                r["stars_today"] = count_stars_today(client, headers, r["full_name"])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "mshibanami/GitHubTrendingRSS (rank) + GitHub REST API (stats) + Events API (24h)",
        "languages": [l[0] for l in RSS_FEEDS],
        "repos": all_results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ Wrote {len(all_results)} repos to {out_path.relative_to(PROJECT_DIR)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())