#!/usr/bin/env python3
"""
GitHub AI Star Tracker — 每日追踪 GitHub AI 项目新增 Star 排行榜

用法：
    python3 track.py              # 运行一次，生成/更新 README.md
    python3 track.py --no-commit  # 只生成报告，不提交到 Git
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"
REPORT_FILE = REPO_DIR / "README.md"

# ── 搜索查询（覆盖主流 AI 方向）────────────────────────────────────────
QUERIES = [
    # 通用 AI
    ("topic:artificial-intelligence stars:>100", 30),
    ("topic:machine-learning stars:>100", 20),
    # 大模型 / LLM
    ("topic:llm stars:>50", 20),
    ("topic:large-language-models stars:>50", 15),
    ("llm+in:name,description+stars:>50+created:>2024-01-01", 15),
    # 生成式 AI
    ("topic:generative-ai stars:>50", 15),
    ("topic:chatgpt stars:>50", 15),
    # Agent / 工具
    ("topic:ai-agent stars:>20", 15),
    ("topic:agent stars:>50+ai+in:name,description", 15),
    # 新星项目（2025 年以后创建的高星项目）
    ("ai+in:name,description+stars:>50+created:>2025-01-01", 20),
    ("topic:deep-learning stars:>100", 15),
    # MCP / 生态
    ("topic:mcp stars:>20", 15),
    ("topic:rag stars:>20", 10),
    # 开源模型
    ("topic:fine-tuning stars:>20", 10),
]


def _get_token() -> str:
    """获取 GitHub token（优先环境变量，其次 gh CLI）。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    raise RuntimeError("无法获取 GitHub token，请设置 GITHUB_TOKEN 或登录 gh")


def gh_api(path: str) -> dict:
    """通过 curl + token 调用 GitHub API（5000 req/h）。"""
    token = _get_token()
    url = f"https://api.github.com{path}"
    result = subprocess.run(
        [
            "curl", "-s", "--connect-timeout", "15", "--max-time", "45",
            "-H", f"Authorization: token {token}",
            "-H", "Accept: application/vnd.github.v3+json",
            url,
        ],
        capture_output=True, text=True, timeout=50,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    if "message" in data and "documentation_url" in data:
        raise RuntimeError(f"GitHub API error: {data['message']}")
    return data


def load_history() -> dict:
    """加载历史快照文件。"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}


def save_history(history: dict):
    """保存历史快照。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def fetch_all_repos() -> list[dict]:
    """从所有查询中拉取 AI 项目，去重。"""
    seen: set[str] = set()
    repos: list[dict] = []

    for query, per_page in QUERIES:
        encoded = quote_plus(query)
        full_path = f"/search/repositories?q={encoded}&sort=stars&order=desc&per_page={per_page}"
        try:
            result = gh_api(full_path)
            time.sleep(0.3)  # 避免触发二级限流
        except Exception as e:
            print(f"  ⚠️  查询失败 [{query[:40]}…]: {e}", file=sys.stderr)
            continue

        for item in result.get("items", []):
            full_name = item["full_name"]
            if full_name not in seen:
                seen.add(full_name)
                repos.append({
                    "full_name": full_name,
                    "stars": item["stargazers_count"],
                    "forks": item["forks_count"],
                    "created_at": item["created_at"],
                    "description": item.get("description") or "",
                    "html_url": item["html_url"],
                    "language": item.get("language") or "",
                    "topics": item.get("topics", []),
                })

    return repos


def compute_increases(repos: list[dict], history: dict) -> tuple[list[dict], dict]:
    """计算每日 Star 增量，更新历史快照。

    对每个 repo：
    - 若历史中有记录，增量 = 当前 stars - 最近一次记录 stars
    - 若首次出现，增量 = None（标记为 NEW）
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for repo in repos:
        fn = repo["full_name"]
        records = history.get(fn, [])

        if records:
            last = records[-1]
            repo["prev_stars"] = last["stars"]
            repo["star_increase"] = repo["stars"] - last["stars"]
            repo["prev_date"] = last["date"]
        else:
            repo["prev_stars"] = None
            repo["star_increase"] = None
            repo["prev_date"] = None

        # 追加今日记录
        if fn not in history:
            history[fn] = []
        # 避免同一天重复记录
        if not history[fn] or history[fn][-1]["date"] != today:
            history[fn].append({"date": today, "stars": repo["stars"]})

    return repos, history


def generate_report(repos: list[dict], history: dict) -> str:
    """生成 Markdown 报告。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── 按今日新增排序 ──
    gainers = sorted(
        repos,
        key=lambda r: (r.get("star_increase") if r.get("star_increase") is not None else -1),
        reverse=True,
    )

    # ── 历史汇总（去重：每个项目只出现一次，记录历史最高日增） ──
    # 从历史数据中计算每个项目的最高日增
    history_summary: dict[str, dict] = {}
    for fn, records in history.items():
        if len(records) < 2:
            continue
        max_inc = 0
        max_date = ""
        for i in range(1, len(records)):
            inc = records[i]["stars"] - records[i - 1]["stars"]
            if inc > max_inc:
                max_inc = inc
                max_date = records[i]["date"]
        history_summary[fn] = {"max_daily_inc": max_inc, "max_inc_date": max_date}

    # ── 组装输出 ──
    lines = [
        "# 🤖 GitHub AI 项目每日新增 Star 排行榜",
        "",
        f"> 📅 更新时间：**{today}**  ",
        "> 🔍 数据来源：[GitHub API](https://api.github.com)（已认证，5000 req/h）  ",
        "> 📊 排序依据：**最近一天新增 Star 数量**（今日 Stars − 上次记录 Stars）  ",
        f"> 📦 追踪项目数：**{len(repos)}**（已去重）",
        "",
        "---",
        "",
        "## 🔥 今日排行 — 按新增 Star 数降序",
        "",
        "| # | 项目 | ⭐ Stars | 🍴 Forks | 📅 创建日期 | 🔥 今日新增 | 📝 完整功能描述 |",
        "|---|------|---------|---------|------------|------------|----------------|",
    ]

    for i, r in enumerate(gainers, 1):
        name = f"[{r['full_name']}]({r['html_url']})"
        stars = f"{r['stars']:,}"
        forks = f"{r['forks']:,}"
        created = r["created_at"][:10] if r["created_at"] else "N/A"
        inc = r.get("star_increase")
        if inc is None:
            inc_str = "🆕 新收录"
        elif inc == 0:
            inc_str = "→ 0"
        elif inc > 0:
            inc_str = f"🔥 **+{inc:,}**"
        else:
            inc_str = f"📉 {inc:,}"
        desc = r["description"].replace("|", "\\|")[:120] or "暂无描述"

        lines.append(f"| {i} | {name} | {stars} | {forks} | {created} | {inc_str} | {desc} |")

    lines.extend([
        "",
        "---",
        "",
        "## 📈 历史汇总（去重 · 每项目一行）",
        "",
        "| 项目 | ⭐ 最新 Stars | 🍴 Forks | 📅 创建日期 | 📊 历史最高日增 | 📅 最高日增日期 | 📝 完整功能描述 |",
        "|------|-------------|---------|------------|---------------|----------------|----------------|",
    ])

    # 按最新 stars 降序排列历史汇总
    history_sorted = sorted(repos, key=lambda r: r["stars"], reverse=True)
    for r in history_sorted:
        fn = r["full_name"]
        name = f"[{fn}]({r['html_url']})"
        stars = f"{r['stars']:,}"
        forks = f"{r['forks']:,}"
        created = r["created_at"][:10] if r["created_at"] else "N/A"
        hs = history_summary.get(fn, {})
        max_inc = f"+{hs['max_daily_inc']:,}" if hs.get("max_daily_inc") else "N/A"
        max_date = hs.get("max_inc_date", "—")
        desc = r["description"].replace("|", "\\|")[:120] or "暂无描述"

        lines.append(f"| {name} | {stars} | {forks} | {created} | {max_inc} | {max_date} | {desc} |")

    lines.extend([
        "",
        "---",
        "",
        f"*🤖 自动生成于 {today} · 数据来源 [GitHub API](https://api.github.com) · 项目地址 [ai-star-tracker](https://github.com/jerizhangWeb3th/ai-star-tracker)*",
    ])

    return "\n".join(lines) + "\n"


def git_commit_and_push():
    """提交并推送到 GitHub（使用 SSH）。"""
    try:
        # 确保使用 SSH remote
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "remote", "set-url", "origin",
             "git@github.com:jerizhangWeb3th/ai-star-tracker.git"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "add", "README.md", "data/history.json"],
            check=True,
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "commit", "-m", f"📊 每日更新 {today}"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "push", "origin", "main"],
            check=True, timeout=60,
        )
        print("✅ 已提交并推送到 GitHub")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Git 操作失败：{e}", file=sys.stderr)


def main():
    no_commit = "--no-commit" in sys.argv

    print("=" * 60)
    print("🤖 GitHub AI Star Tracker")
    print("=" * 60)

    # 1. 拉取数据
    print("\n📡 正在从 GitHub API 拉取 AI 项目数据…")
    repos = fetch_all_repos()
    print(f"   ✅ 获取到 {len(repos)} 个不重复 AI 项目")

    # 2. 加载历史
    print("\n📂 加载历史快照…")
    history = load_history()
    print(f"   ✅ 已有 {len(history)} 个项目的历史记录")

    # 3. 计算增量
    print("\n🧮 计算 Star 增量…")
    repos, history = compute_increases(repos, history)

    gainers = sorted(
        repos,
        key=lambda r: (r.get("star_increase") if r.get("star_increase") is not None else -1),
        reverse=True,
    )
    print(f"\n   🏆 今日新增 Top 5：")
    for r in gainers[:5]:
        inc = r.get("star_increase")
        inc_str = f"+{inc:,}" if inc else "NEW"
        print(f"      {inc_str:>12}  {r['full_name']}")

    # 4. 生成报告
    print("\n📝 生成 Markdown 报告…")
    report = generate_report(repos, history)
    REPORT_FILE.write_text(report)
    print(f"   ✅ 已写入 {REPORT_FILE}")

    # 5. 保存历史
    print("\n💾 保存历史快照…")
    save_history(history)
    print(f"   ✅ 已保存 {len(history)} 个项目记录")

    # 6. 提交推送
    if not no_commit:
        print("\n🚀 提交并推送到 GitHub…")
        git_commit_and_push()

    print("\n" + "=" * 60)
    print("✅ 完成！")
    print(f"📄 报告地址: https://github.com/jerizhangWeb3th/ai-star-tracker")
    print("=" * 60)


if __name__ == "__main__":
    main()
