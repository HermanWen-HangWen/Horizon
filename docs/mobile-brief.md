---
layout: default
title: Mobile Daily Brief Pipeline
---

# Mobile Daily Brief Pipeline

## 概述

将 Horizon 的长原始简报(508 行)改写为 ≤60 行的移动端友好版,适合通勤路上手机快速扫读。AI 聚焦于 AI/科技主题;财经、政治、军事、低分内容全部丢弃。

**输入**: `horizon --hours 24` 生成的 `data/summaries/horizon-YYYY-MM-DD-zh.md`(~30 KB markdown)

**输出**: `techradar/每日AI科技简报 — YYYY-MM-DD.md`(≤60 行,emoji 分节,带源标签)

---

## 流水线架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Daily Run Flow                             │
└─────────────────────────────────────────────────────────────────────┘

[1] horizon --hours 24
    ├─ Scrapers: GitHub, Hacker News, RSS, Reddit, OSS Insight
    ├─ AI 分析 + 分类 + 去重
    ├─ 生成 data/summaries/horizon-YYYY-MM-DD-zh.md (508 行)
    └─ 生成 docs/_posts/YYYY-MM-DD-summary-zh.md (GitHub Pages)

[2] fetch_trending.py  (独立脚本,数据互补)
    ├─ mshibanami/GitHubTrendingRSS → 排名顺序 (GitHub 官方 trending)
    ├─ /repos/{owner}/{repo}        → 总 star + 描述 + 语言
    ├─ /repos/{owner}/{repo}/events → 数 24h 内 WatchEvent = +X today
    └─ 写入 data/trending.json (gitignored)

[3] mobile-digest.py  (后处理脚本)
    ├─ 读取 [1] 的 raw summary
    ├─ 读取 [2] 的 trending.json
    ├─ 拼装 user prompt(含 Trending 真实数据段)
    ├─ MiniMax-M3 改写 + 过滤 AI 相关
    └─ 写入 techradar/每日AI科技简报 — YYYY-MM-DD.md
```

---

## Trending 数据源

**GitHub Trending 排序 = mshibanami/GitHubTrendingRSS 的 item 顺序**

理由:GitHub 官方 `/trending` 页面是 HTML 渲染,没有公开 API。mshibanami 通过 GitHub Actions 每天抓 trending 页面并发布为 RSS daily rollup,**item 顺序就是 GitHub 官方 trending 的排名**。

三份 RSS feed(各取 top 10):

| 标签 | URL | 用途 |
|---|---|---|
| `all` | `https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml` | 全语言 trending |
| `python` | `.../daily/python.xml` | Python 生态 trending |
| `typescript` | `.../daily/typescript.xml` | JS/TS 生态 trending |

**为什么 RSS 需要 channel-date fallback**

mshibanami RSS 的每个 item **没有自己的 `<pubDate>`**,只有 channel 级别有。Horizon 原 `RSSScraper._parse_date()` 要求每个 item 都有日期才能过 `since` 过滤,否则全部丢弃。

修复:在 `RSSSourceConfig` 加 `daily_rollup: bool = False` 字段,True的 `_fetch_feed` 会把 `feed.feed.published_parsed` 作为 fallback 日期。仅对三份 trending RSS 开启。

`src/scrapers/rss.py`:
```python
published_at = self._parse_date(entry, feed, use_channel_fallback=source.daily_rollup)
```

`src/models.py`:
```python
class RSSSourceConfig(BaseModel):
    ...
    daily_rollup: bool = False  # channel-date fallback opt-in
```

---

## Trending 富化:总 star + 今日新增

mshibanami RSS 只给 `owner/repo` 标题和 README 摘要,**没有 star 数据**。需补两次 GitHub API:

| 数据 | API | 调用 |
|---|---|---|
| `total_stars` | `GET /repos/{owner}/{repo}` | 1 次/repo |
| `stars_today` | `GET /repos/{owner}/{repo}/events` 数 WatchEvent | 1 次/repo |

**为什么用 Events API 不用 Stargazers API 数时间戳**

`/repos/{owner}/{repo}/stargazers` 配 `Accept: application/vnd.github.v3.star+json` 会返回每个 star 的 `starred_at` 时间戳(更精确)。**但需要 token 有 `public_repo` scope**(classic)或对应 fine-grained 权限。

用户的 GitHub token 没有这个 scope(测试返回 403 "Resource not accessible by personal access token")。Events API 用普通 OAuth scope 即可,且最近 300 个事件中 WatchEvent 占比高,对 trending repo 足够准。

**调用次数**:30 个 repo × 2 = 60 次/次。在 5000/h 限额内。

---

## AI 过滤逻辑

MiniMax-M3 在 LLM 阶段做过滤,**不**预先筛数据:

1. prompt 末尾的 user message 包含完整 Trending 真实数据(30 行,所有 repo)
2. system prompt 第 9 条指示 LLM:
   - 过滤关键词:LLM / agent / model / 训练 / 推理 / embedding / 向量 / prompt / RAG / Claude / GPT / Qwen / Llama / Mistral / diffusion
   - 按 `stars_today` 倒序排
   - 不足 3 条就少写,不凑数

```python
# scripts/mobile-digest.py:51-56
9. **GitHub Trending 段:从 user prompt 末尾的"Trending 真实数据"段里,
   过滤出 AI 相关的 repo,选 Top 3**
   - 过滤标准:描述/名称包含 LLM、agent、model、训练、推理、embedding、
     向量、prompt、RAG、Claude、GPT、Qwen、Llama、Mistral、diffusion 等
   - 排序:**按 stars_today 倒序**
   - 数据严格使用该段提供的 total_stars 和 stars_today
   - 若过滤后少于 3 条 AI repo,有多少写多少,不要凑数
```

**为什么在 LLM 里过滤而不是脚本里**

- LLM 能识别模糊语义(如 "训练流水线"、"推理服务器"、"向量数据库" 这些不在关键词表里但明显 AI 的描述)
- 关键词黑名单会漏掉新出现的术语(如 "MoE"、"context window")
- LLM 已经在消化 raw summary,顺手过滤 trending 几乎零成本

---

## 输出示例

```markdown
# 每日AI科技简报 — 2026-09-02

> 来源: HackerNews, Simon Willison, Latent Space, Wired, GitHub Trending, ...
> 采集时间: 2026-09-02 16:00 UTC

## 📌 今日热点 (Top 5)
1. **Google DeepMind 发布 Gemini 3.8 Flash** — [HN] ... — https://...
2. **Paint.NET 作者用 AI 重写 Direct2D** — [Simon Willison] ... — https://...
3. **OpenAI Astra 触及"关键"网络安全能力** — [Wired] ... — https://...
4. **NVIDIA 开源 SkillSpector** — [GitHub] ... — https://...
5. **Claude Fable/Mythos 5.1** — [Latent Space] ... — https://...

## 🚀 平台与产品动态
- ...

## 💻 模型与基础设施
- ...

## 📊 GitHub Trending Top 3 (AI 相关,按今日 stars)
1. **academic-research-skills** — Python — Claude Code 学术研究技能链 — ⭐ 45.7k / 今日 +96
2. **humanizer** — Python — Agent 技能:消除文本中的 AI 生成痕迹 — ⭐ 40.7k / 今日 +95
3. **superlinked/sie** — Python — 开源推理服务器 — ⭐ 3.1k / 今日 +95

## ⚠️ 数据可获得性
- HackerNews: ✅ 正常,7条科技新闻抓取完整
- GitHub Trending: ✅ 三语言榜单完整
- 财经/政治/军事 (The Economist 13 条): 已按规则全部丢弃
```

---

## 相关文件

| 文件 | 角色 |
|---|---|
| `scripts/mobile-digest.py` | 后处理主脚本 |
| `scripts/fetch_trending.py` | 独立抓 trending (RSS + API) |
| `data/trending.json` | 富化后的 trending 数据 (gitignored) |
| `data/summaries/horizon-YYYY-MM-DD-zh.md` | horizon 原始简报 (gitignored) |
| `src/scrapers/rss.py` | RSS scraper (含 channel-date fallback) |
| `src/models.py` | `RSSSourceConfig.daily_rollup` 字段 |
| `data/config.json` | `sources.rss` 三份 trending RSS 配 `daily_rollup: true` |

---

## 运行方式

```bash
# 完整 pipeline(自动跑 horizon,如缺 summary)
uv run python scripts/mobile-digest.py

# 指定日期,不重跑 horizon(快速迭代 prompt 用)
uv run python scripts/mobile-digest.py --date 2026-09-02 --no-fetch

# 只刷新 trending.json(改 prompt 后避免重跑整个 horizon)
uv run python scripts/fetch_trending.py
```

---

## 已知限制

- **Events API 只覆盖最近 300 个事件**:超级热 repo(单日 100+ stars)会低估 `+X today`
- **mshibanami RSS 顺序可能与 github.com/trending 有 ~1h 滞后**(RSS 由 GitHub Actions 周期生成)
- **AI 过滤依赖 LLM 判断**:偶尔会把边缘 AI 项目(比如只是用 Python 写了 ML 工具)误判/漏判
- **没有 webhook 推送**:目前需要用户主动开文件读;Bark / Telegram / Server酱 webhook 待集成