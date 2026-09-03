#!/usr/bin/env python3
"""Generate a mobile-friendly daily digest from Horizon's raw summary.

Usage:
    uv run python scripts/mobile-digest.py            # today
    uv run python scripts/mobile-digest.py --date 2026-09-02
    uv run python scripts/mobile-digest.py --no-fetch # don't auto-run horizon

Output:
    <output-dir>/每日AI科技简报 — YYYY-MM-DD.md
    (default output-dir: parent of Horizon repo, i.e. /Users/wenhang/study/techradar/)
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Resolve project paths so the script works from any cwd.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
SUMMARIES_DIR = DATA_DIR / "summaries"
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR.parent  # /Users/wenhang/study/techradar

# Load .env before importing horizon modules that read env vars at import time.
load_dotenv(PROJECT_DIR / ".env")

sys.path.insert(0, str(PROJECT_DIR))

from src.ai.client import _create_single_client  # noqa: E402
from src.models import AIConfig, AIProvider  # noqa: E402


SYSTEM_PROMPT = """\
你是移动端科技简报编辑。任务:把 Horizon 系统抓取的原始英文/中文简报,改写成适合**通勤路上手机阅读**的精简版。

严格要求:
1. **总长度 ≤ 60 行**(手机一屏约 30 行,60 行是上下滑动的合理上限)
2. 用 emoji 分节符号(📌🚀💻📦📊⚠️ 等)便于扫读,不要用大段文字
3. 每条新闻保留**源头标签**:`[HN]`、`[TechCrunch]`、`[Follow Builders]`、`[GitHub]` 等,可多个并列 `[HN + TechCrunch]`
4. 按热度/重要性排序,**只保留最重要的 5-10 条**;低分(<5)、重复、推测性内容全部丢弃
5. 每条用简短编辑评注解释"为什么重要",一句话即可
6. **完全删除**: 财经/政治/军事新闻、广告、PR 内容、模糊无来源的传闻
7. 末尾保留一段"⚠️ 数据可获得性"说明,简述哪些源正常/失败/数据空
8. 链接紧跟在标题后,一行内展示;不要把链接单独成行占用屏幕

输出格式(必须严格遵循):
```
# 每日AI科技简报 — YYYY-MM-DD

> 来源: <列出实际抓取到的源,用逗号分隔>
> 采集时间: YYYY-MM-DD HH:MM <时区>

## 📌 今日热点(Top 5)
1. **<标题>** — [<源标签>] <一句话编辑评注> — <链接>

## 🚀 平台与产品动态
- **<标题>** — [<源标签>] <一句话> — <链接>

## 💻 模型与基础设施
- ...

## 📊 GitHub Trending Top 3
1. **<repo>** — <语言> — <一句话> — ⭐ <star> / 今日 +<delta>
2. ...

## ⚠️ 数据可获得性
- <源1>: <状态>
- <源2>: <状态>
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


async def generate_digest(client, raw: str, date: str) -> str:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        date=date,
        raw_lines=raw.count("\n") + 1,
        raw_content=raw,
    )
    return await client.complete(
        system=SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.4,  # slightly higher for editorial flair
    )


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
    digest = asyncio.run(generate_digest(client, raw, date))

    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"每日AI科技简报 — {date}.md"
    out_path.write_text(digest.strip() + "\n", encoding="utf-8")

    out_lines = digest.count("\n") + 1
    print(f"✅ Wrote {out_path} ({out_lines} lines)", file=sys.stderr)
    if out_lines > args.max_lines:
        print(f"⚠️  Output is {out_lines} lines, exceeds target {args.max_lines}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())