#!/usr/bin/env python3
"""Fetch GitHub trending repos via the Search API and save as JSON.

Uses the official GitHub REST API (Search Repositories endpoint) which
returns rich JSON including star counts, language, description, and topics.
This is GitHub's official alternative to the (HTML-only) trending page.

Output: data/trending.json

Usage:
    uv run python scripts/fetch_trending.py
    uv run python scripts/fetch_trending.py --days 7 --min-stars 50 --per-language 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_PATH = DATA_DIR / "trending.json"

# Load .env before reading GITHUB_TOKEN.
load_dotenv(PROJECT_DIR / ".env")

GITHUB_API = "https://api.github.com/search/repositories"
LANGUAGES = [("all", ""), ("python", "+language:python"), ("typescript", "+language:typescript")]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="How far back to look for repo creation (default: 7)")
    p.add_argument("--min-stars", type=int, default=50, help="Minimum total stars (default: 50)")
    p.add_argument("--per-language", type=int, default=10, help="Top N per language (default: 10)")
    p.add_argument("--output", default=str(OUTPUT_PATH), help="Output JSON path")
    return p.parse_args()


def fetch_category(client: httpx.Client, headers: dict, days: int, min_stars: int, per_page: int, label: str, lang_suffix: str) -> list:
    """Fetch one language category from the Search API."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    # Build URL manually — httpx's params dict over-encodes `+` as `%2B`,
    # which GitHub's API does not decode back to space. Use `%3E` for `>`
    # and `+` for space (matches GitHub's expected query syntax).
    query = f"created:>{since}+stars:>{min_stars}{lang_suffix}"
    url = f"{GITHUB_API}?q={query}&sort=stars&order=desc&per_page={per_page}"
    try:
        resp = client.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"⚠️  {label}: {e}", file=sys.stderr)
        return []

    items = resp.json().get("items", [])
    results = []
    for rank, item in enumerate(items, start=1):
        results.append({
            "rank": rank,
            "language": label,
            "full_name": item["full_name"],
            "url": item["html_url"],
            "description": (item.get("description") or "").strip(),
            "primary_language": item.get("language") or "Unknown",
            "total_stars": item.get("stargazers_count", 0),
            "forks": item.get("forks_count", 0),
            "created_at": item.get("created_at", ""),
            "pushed_at": item.get("pushed_at", ""),
            "topics": item.get("topics", [])[:5],
        })
    return results


def count_stars_today(client: httpx.Client, headers: dict, full_name: str) -> int:
    """Count WatchEvents (stars) in last 24h via the Events API.

    Uses /repos/{owner}/{repo}/events which doesn't require the special
    `star+json` accept header (works with default OAuth scopes). Returns
    0 on failure. Capped at 1 page since GitHub events endpoint serves
    the 300 most recent events and WatchEvents dominate for trending repos.
    """
    url = f"https://api.github.com/repos/{full_name}/events?per_page=100"
    try:
        resp = client.get(url, headers=headers, timeout=30.0)
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
        for label, lang_suffix in LANGUAGES:
            print(f"🔍 Fetching {label}…", file=sys.stderr)
            items = fetch_category(
                client, headers,
                days=args.days, min_stars=args.min_stars,
                per_page=args.per_language,
                label=label, lang_suffix=lang_suffix,
            )
            print(f"   → {len(items)} repos", file=sys.stderr)
            all_results.extend(items)

        # Enrich with stars_today via the Events API (one extra call per repo).
        if all_results:
            print(f"⭐ Counting 24h stars for {len(all_results)} repos…", file=sys.stderr)
            for r in all_results:
                r["stars_today"] = count_stars_today(client, headers, r["full_name"])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "min_stars": args.min_stars,
        "languages": [l[0] for l in LANGUAGES],
        "repos": all_results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ Wrote {len(all_results)} repos to {out_path.relative_to(PROJECT_DIR)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())