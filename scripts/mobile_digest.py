#!/usr/bin/env python3
"""Generate a mobile-friendly daily digest from Horizon's raw summary.

Usage:
    uv run mobile_digest            # today
    uv run mobile_digest --date 2026-09-02
    uv run mobile_digest --no-fetch # don't auto-run horizon
    uv run mobile_digest --no-push  # write file only, skip nezha push

Output:
    <output-dir>/每日AI科技简报 — YYYY-MM-DD.md
    (default output-dir: <Horizon repo>/docs/_posts/, so the daily-summary
    workflow's GitHub Pages publish step picks it up alongside the Horizon
    summaries)

Push:
    After writing the digest, push it to the nezha-agenthome inbound API.
    Requires env vars TECHNAR_TOKEN (X-Agent-Key) and TECHNAR_USERNAMES
    (comma-separated recipients). Both missing -> push is skipped with a warning.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Resolve project paths so the script works from any cwd.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
SUMMARIES_DIR = DATA_DIR / "summaries"
CONFIG_PATH = DATA_DIR / "config.json"
TRENDING_PATH = DATA_DIR / "trending.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "docs" / "_posts"

# Load .env before importing horizon modules that read env vars at import time.
load_dotenv(PROJECT_DIR / ".env")

sys.path.insert(0, str(PROJECT_DIR))

from src.ai.client import _create_single_client  # noqa: E402
from src.models import AIConfig, AIProvider  # noqa: E402


SYSTEM_PROMPT = """\
你是移动端科技简报编辑。任务:把 Horizon 系统抓取的原始英文/中文简报,改写成适合**通勤路上手机阅读**的精简版。

严格要求:
1. **总长度 ≤ 60 行**(手机一屏约 30 行,60 行是上下滑动的合理上限)
2. **只使用** 📌🚀💻📊 四个 emoji 分节,便于扫读,不要用大段文字。**不要新增其他 section**(🛠、🔧、🎯 等都不行);无法清晰归入任何一节的内容,按你的判断合并到最贴近的那一节
3. 每条新闻保留**源头标签**:`[HN]`、`[TechCrunch]`、`[Follow Builders]`、`[GitHub]` 等,可多个并列 `[HN + TechCrunch]`
4. 按热度/重要性排序,**只保留最重要的 5-10 条**;低分(<5)、重复、推测性内容全部丢弃
5. 每条用简短编辑评注解释"为什么重要",一句话即可
6. **完全删除**: 财经/政治/军事新闻、广告、PR 内容、模糊无来源的传闻;以及"数据可获得性"等元信息段
7. **链接格式**:统一用 markdown 链接 `[详情](URL)`,不要裸 URL。例如:`[详情](https://example.com/foo)`

8. **GitHub Trending 段:从 user prompt 末尾的"Trending 真实数据"段里,过滤出 AI 相关的 repo,选 Top 3**
   - 过滤标准:描述/名称包含 LLM、agent、model、训练、推理、embedding、向量、prompt、RAG、Claude、GPT、Qwen、Llama、Mistral、diffusion 等 AI 关键词,或主语言是 Python 且与 AI 生态相关
   - 排序:**按 stars_today 倒序**(用户想看"今天涨最快的 AI repo",不是总 star 最多的)
   - 数据严格使用该段提供的 total_stars 和 stars_today
   - 若过滤后少于 3 条 AI repo,有多少写多少,不要凑数
   - 链接同样用 `[详情](https://github.com/owner/repo)` 格式

输出格式(直接按下面这个示例产出 markdown,**不要**再用 ``` 包起来):
# 每日AI科技简报 — YYYY-MM-DD

(空一行)

## 📌 今日热点(Top 5)
1. **<标题>** — [<源标签>] <一句话编辑评注> [详情](<URL>)

## 🚀 平台与产品动态
- **<标题>** — [<源标签>] <一句话> [详情](<URL>)

## 💻 模型与基础设施
- ...

## 📊 GitHub Trending Top 3 (AI 相关,按今日 stars)
1. **<repo>** — <语言> — <一句话> — ⭐ <star> / 今日 +<delta> [详情](<URL>)
2. ...

(空一行)

---
> 来源: <列出实际抓取到的源,用逗号分隔>
> 采集时间: YYYY-MM-DD HH:MM <时区>
```

只输出 markdown,不要任何前言或解释。\
"""

USER_PROMPT_TEMPLATE = """\
以下是 Horizon 今天抓取的原始简报(可能很长)。请按系统提示要求精简改写。

日期:{date}
原始报告长度:{raw_lines} 行

========== 原始简报开始 ==========
{raw_content}
========== 原始简报结束 ==========

{trending_block}

请输出精简版(目标 ≤ 60 行)。\
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", help="YYYY-MM-DD (default: today UTC)")
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Don't auto-run `uv run horizon --hours 24` if summary missing",
    )
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    p.add_argument("--language", default="zh", choices=["zh", "en"], help="Summary language")
    p.add_argument("--max-lines", type=int, default=60, help="Target max lines for digest")
    p.add_argument(
        "--no-push",
        action="store_true",
        help="Write the digest file but skip the nezha-agenthome push",
    )
    return p.parse_args()


def find_summary(date: str, language: str) -> Path | None:
    candidate = SUMMARIES_DIR / f"horizon-{date}-{language}.md"
    return candidate if candidate.exists() else None


def run_horizon() -> None:
    print("📥 Today's summary missing — running `uv run horizon --hours 24` …", file=sys.stderr)
    subprocess.run(
        ["uv", "run", "horizon", "--hours", "24"],
        cwd=PROJECT_DIR,
        check=True,
    )


def load_ai_config() -> AIConfig:
    """Read data/config.json and extract the ai block into AIConfig."""
    import json

    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    return AIConfig(**cfg["ai"])


def load_trending_block() -> str:
    """Build the 'Trending 真实数据' block for the user prompt.

    Auto-fetches via scripts/fetch_trending.py if data/trending.json is missing.
    Returns an empty block string if the fetch fails or produces no data.
    """
    import json

    if not TRENDING_PATH.exists():
        print("📈 Trending data missing — running fetch_trending.py …", file=sys.stderr)
        result = subprocess.run(
            ["uv", "run", "python", str(SCRIPT_DIR / "fetch_trending.py")],
            cwd=PROJECT_DIR,
            check=False,
        )
        if result.returncode != 0 or not TRENDING_PATH.exists():
            print("⚠️  Trending fetch failed; will skip the Trending section.", file=sys.stderr)
            return ""

    try:
        payload = json.loads(TRENDING_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️  Trending JSON unreadable: {exc}", file=sys.stderr)
        return ""

    repos = payload.get("repos", [])
    if not repos:
        return ""

    lines = ["========== Trending 真实数据 (GitHub Search + Events API) =========="]
    lines.append(
        f"窗口:最近 {payload.get('window_days', '?')} 天 · 最低 star: {payload.get('min_stars', '?')} · 生成时间: {payload.get('generated_at', '?')}"
    )
    lines.append(
        "每条格式:rank · full_name · primary_language · total_stars · stars_today(24h) · description"
    )
    lines.append("")
    for r in repos:
        desc = (r.get("description") or "(无描述)").replace("\n", " ").strip()
        today = r.get("stars_today", 0)
        lines.append(
            f"#{r['rank']} [{r['language']}] {r['full_name']} | {r['primary_language']} | ⭐{r['total_stars']} | +{today} today | {desc} | {r['url']}"
        )
    lines.append("========== Trending 数据结束 ==========")
    return "\n".join(lines)


async def generate_digest(client, raw: str, date: str, trending_block: str) -> str:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        date=date,
        raw_lines=raw.count("\n") + 1,
        raw_content=raw,
        trending_block=trending_block or "(Trending 数据不可用,跳过 📊 GitHub Trending 段)",
    )
    return await client.complete(
        system=SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.4,  # slightly higher for editorial flair
    )


def push_to_nezha(digest: str, date: str) -> None:
    """Push the digest to the nezha-agenthome inbound API.

    Reads TECHNAR_TOKEN (X-Agent-Key) and TECHNAR_USERNAMES (comma-separated)
    from the environment. Either missing -> push is skipped with a warning;
    failures are logged but do not raise, so the local digest file is
    always produced regardless of push outcome.
    """
    token = os.getenv("TECHNAR_TOKEN")
    usernames_raw = os.getenv("TECHNAR_USERNAMES", "")

    if not token:
        print("⚠️  TECHNAR_TOKEN not set; skipping nezha push", file=sys.stderr)
        return
    if not usernames_raw:
        print("⚠️  TECHNAR_USERNAMES not set; skipping nezha push", file=sys.stderr)
        return

    usernames = [u.strip() for u in usernames_raw.split(",") if u.strip()]
    if not usernames:
        print("⚠️  TECHNAR_USERNAMES is empty; skipping nezha push", file=sys.stderr)
        return

    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    payload = {
        "source_agent_id": "techradar",
        "idempotency_key": f"msg-{date}-{timestamp}",
        "routing": {
            "type": "explicit",
            "usernames": usernames,
        },
        "message": {
            "parts": [
                {
                    "kind": "text",
                    "text": digest,
                }
            ]
        },
    }

    url = "https://nezha-agenthome.cn-pgcloud.com/api/a2a/inbound/push/async"
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Key": token,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
        delivered = result.get("success_count", "?")
        total = result.get("total_count", "?")
        print(
            f"📤 Pushed to nezha: status={result.get('status')} "
            f"request_id={result.get('request_id')} "
            f"delivered={delivered}/{total}",
            file=sys.stderr,
        )
    except httpx.HTTPStatusError as e:
        print(
            f"⚠️  Nezha push HTTP {e.response.status_code}: {e.response.text[:300]}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"⚠️  Nezha push failed: {type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    summary_path = find_summary(date, args.language)
    if summary_path is None:
        if args.no_fetch:
            print(f"❌ Summary not found: {SUMMARIES_DIR}/horizon-{date}-{args.language}.md", file=sys.stderr)
            print("   Run `uv run horizon --hours 24` first, or drop --no-fetch.", file=sys.stderr)
            return 1
        run_horizon()
        summary_path = find_summary(date, args.language)
        if summary_path is None:
            print(f"❌ Horizon finished but summary still missing for {date}", file=sys.stderr)
            return 1

    raw = summary_path.read_text(encoding="utf-8")
    print(f"📄 Read {len(raw)} chars from {summary_path.relative_to(PROJECT_DIR)}", file=sys.stderr)

    try:
        ai_config = load_ai_config()
    except FileNotFoundError:
        print(f"❌ Config not found: {CONFIG_PATH}", file=sys.stderr)
        return 1

    if ai_config.provider != AIProvider.MINIMAX:
        print(f"⚠️  Config provider is {ai_config.provider.value}, not minimax. Continuing anyway.", file=sys.stderr)

    print(f"🤖 Calling {ai_config.provider.value}/{ai_config.model} …", file=sys.stderr)
    client = _create_single_client(ai_config)
    trending_block = load_trending_block()
    digest = asyncio.run(generate_digest(client, raw, date, trending_block))

    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"每日AI科技简报 — {date}.md"
    out_path.write_text(digest.strip() + "\n", encoding="utf-8")

    out_lines = digest.count("\n") + 1
    print(f"✅ Wrote {out_path} ({out_lines} lines)", file=sys.stderr)
    if out_lines > args.max_lines:
        print(f"⚠️  Output is {out_lines} lines, exceeds target {args.max_lines}", file=sys.stderr)

    if args.no_push:
        print("⏭️  --no-push set; skipping nezha push", file=sys.stderr)
    else:
        push_to_nezha(digest.strip(), date)

    return 0


if __name__ == "__main__":
    sys.exit(main())