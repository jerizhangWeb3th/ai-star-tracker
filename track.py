#!/usr/bin/env python3
"""
GitHub AI Star Tracker — 每日追踪 GitHub AI 项目新增 Star 排行榜

用法：
    python3 track.py              # 运行一次，生成/更新 README.md
    python3 track.py --no-commit  # 只生成报告，不提交到 Git
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"
TOP20_FILE = DATA_DIR / "top20_history.json"  # 历史汇总：所有上过榜的项目
TRANSLATION_CACHE_FILE = DATA_DIR / "translations.json"
REPORT_FILE = REPO_DIR / "README.md"
SSH_CONFIG_FILE = Path("/tmp/gh_ssh_config")

TOP_N = 20  # 每日只取前 20 名

# ── SSH 配置（绕过 DNS 污染）─────────────────────────────────────────
def ensure_ssh_config():
    """确保 SSH config 文件存在，使 git push 能绕过 DNS 污染。"""
    if not SSH_CONFIG_FILE.exists():
        SSH_CONFIG_FILE.write_text(
            "Host github.com\n"
            "    HostName 140.82.113.3\n"
            "    Port 22\n"
            "    User git\n"
            "    IdentityFile ~/.ssh/id_rsa\n"
            "    StrictHostKeyChecking accept-new\n"
        )

# ── 翻译 ──────────────────────────────────────────────────────────────
# 判断是否含中文（含则跳过翻译）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

def _has_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _load_translations() -> dict:
    if TRANSLATION_CACHE_FILE.exists():
        with open(TRANSLATION_CACHE_FILE) as f:
            return json.load(f)
    return {}


def _save_translations(cache: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRANSLATION_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


PROXY_URL = "http://pusai123:792963711qq@80.174.220.26:12326"

def translate_desc(text: str) -> str:
    """把描述翻译成中文，已有中文则原样返回。走代理访问 MyMemory 免费 API。"""
    text = text.strip()
    if not text or _has_chinese(text):
        return text

    cache = _load_translations()
    if text in cache:
        return cache[text]

    # 保存原代理设置，翻译完后恢复（避免污染 git push 等后续操作）
    _saved_http = os.environ.get("HTTP_PROXY")
    _saved_https = os.environ.get("HTTPS_PROXY")
    try:
        os.environ["HTTP_PROXY"] = PROXY_URL
        os.environ["HTTPS_PROXY"] = PROXY_URL
        from deep_translator import MyMemoryTranslator
        t = MyMemoryTranslator(source="en-GB", target="zh-CN")
        result = t.translate(text)
        if result and result != text:
            cache[text] = result
            _save_translations(cache)
            return result
    except Exception:
        pass
    finally:
        if _saved_http is not None:
            os.environ["HTTP_PROXY"] = _saved_http
        else:
            os.environ.pop("HTTP_PROXY", None)
        if _saved_https is not None:
            os.environ["HTTPS_PROXY"] = _saved_https
        else:
            os.environ.pop("HTTPS_PROXY", None)

    return text

# ── 搜索查询（覆盖主流 AI 方向）────────────────────────────────────────
QUERIES = [
    # 通用 AI
    ("topic:artificial-intelligence stars:>100", 15),
    ("topic:machine-learning stars:>100", 10),
    # 大模型 / LLM
    ("topic:llm stars:>50", 20),
    ("topic:large-language-models stars:>50", 10),
    ("llm+in:name,description+stars:>50+created:>2024-01-01", 10),
    # 生成式 AI
    ("topic:generative-ai stars:>50", 10),
    ("topic:chatgpt stars:>50", 10),
    # Agent / 工具
    ("topic:ai-agent stars:>20", 15),
    ("topic:agent stars:>50 ai in:name,description", 10),
    # 新星项目（2025 年以后创建的高星项目）
    ("ai+in:name,description+stars:>50+created:>2025-01-01", 15),
    ("topic:deep-learning stars:>100", 10),
    # MCP / 生态
    ("topic:mcp stars:>20", 15),
    ("topic:rag stars:>20", 10),
    # 开源模型
    ("topic:fine-tuning stars:>20", 10),
]


# GitHub API 真实 IP（绕过 DNS 污染）
_GITHUB_API_IPS = ["140.82.113.6", "140.82.114.5", "140.82.113.5"]
_GITHUB_API_IP = None

def _get_api_ip() -> str:
    """测试并返回一个可用的 GitHub API IP。"""
    global _GITHUB_API_IP
    if _GITHUB_API_IP:
        return _GITHUB_API_IP
    import subprocess as _sp
    for ip in _GITHUB_API_IPS:
        r = _sp.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", "8",
             "--resolve", f"api.github.com:443:{ip}",
             "-o", "/dev/null", "-w", "%{http_code}",
             "https://api.github.com"],
            capture_output=True, text=True, timeout=10,
        )
        if r.stdout.strip() == "200":
            _GITHUB_API_IP = ip
            return ip
    return _GITHUB_API_IPS[0]  # 默认返回第一个

def gh_api(path: str) -> dict:
    """通过 curl + --resolve 调用 GitHub API（绕过 DNS 污染），带重试。"""
    api_ip = _get_api_ip()
    url = f"https://api.github.com{path}"

    for attempt in range(2):
        cmd = ["curl", "-s", "--connect-timeout", "10", "--max-time", "25",
               "--resolve", f"api.github.com:443:{api_ip}",
               "-H", "Accept: application/vnd.github.v3+json"]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if "message" in data and "documentation_url" in data:
                raise RuntimeError(f"GitHub API error: {data['message']}")
            return data
        if attempt < 1:
            time.sleep(3)
    raise RuntimeError(f"curl failed (rc={result.returncode}): {result.stderr[:200]}")


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
            time.sleep(1.5)  # 已认证 API，间隔 1.5s
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


def load_top20_history() -> list[dict]:
    """加载所有历史上过 Top 20 的项目。"""
    if TOP20_FILE.exists():
        with open(TOP20_FILE) as f:
            return json.load(f)
    return []


def save_top20_history(items: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOP20_FILE, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def update_top20_history(todays_top20: list[dict], existing: list[dict]) -> list[dict]:
    """将今日 Top 20 合并到历史汇总（同名项目更新，新项目追加）。"""
    by_name = {r["full_name"]: r for r in existing}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for r in todays_top20:
        fn = r["full_name"]
        inc = r.get("star_increase") or 0
        if fn in by_name:
            by_name[fn]["stars"] = r["stars"]
            by_name[fn]["forks"] = r["forks"]
            by_name[fn]["last_seen"] = today
            if inc > by_name[fn].get("max_daily_inc", 0):
                by_name[fn]["max_daily_inc"] = inc
                by_name[fn]["max_inc_date"] = today
        else:
            r_copy = {
                "full_name": r["full_name"],
                "stars": r["stars"],
                "forks": r["forks"],
                "created_at": r["created_at"],
                "description": r["description"],
                "html_url": r["html_url"],
                "max_daily_inc": inc,
                "max_inc_date": today,
                "last_seen": today,
            }
            by_name[fn] = r_copy
    return sorted(by_name.values(), key=lambda r: r.get("stars", 0), reverse=True)


def generate_report(top20: list[dict], top20_history: list[dict]) -> str:
    """生成 Markdown 报告：今日 Top 20 + 历史去重汇总。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── 今日排行（已按新增降序） ──
    lines = [
        "# 🤖 GitHub AI 项目每日新增 Star 排行榜",
        "",
        f"> 📅 更新时间：**{today}**  ",
        "> 🔍 数据来源：[GitHub API](https://api.github.com)（已认证）  ",
        f"> 📊 排序依据：**最近一天新增 Star 数量**，取前 **{TOP_N}** 名  ",
        f"> 📦 历史累计追踪：**{len(top20_history)}** 个项目",
        "",
        "---",
        "",
        f"## 🔥 今日 Top {TOP_N} — 按新增 Star 数降序",
        "",
        "| # | 项目 | ⭐ Stars | 🍴 Forks | 📅 创建日期 | 🔥 今日新增 | 📝 完整功能描述 |",
        "|---|------|---------|---------|------------|------------|----------------|",
    ]

    for i, r in enumerate(top20, 1):
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
        desc_raw = r.get("description") or "暂无描述"
        desc = translate_desc(desc_raw).replace("|", "\\|")[:200]

        lines.append(f"| {i} | {name} | {stars} | {forks} | {created} | {inc_str} | {desc} |")

    lines.extend([
        "",
        "---",
        "",
        "## 📈 历史汇总（去重 · 累计上榜项目）",
        "",
        "| 项目 | ⭐ 最新 Stars | 🍴 Forks | 📅 创建日期 | 📊 历史最高日增 | 📅 最高日增日期 | 📝 完整功能描述 |",
        "|------|-------------|---------|------------|---------------|----------------|----------------|",
    ])

    for r in top20_history:
        name = f"[{r['full_name']}]({r['html_url']})"
        stars = f"{r['stars']:,}"
        forks = f"{r['forks']:,}"
        created = r["created_at"][:10] if r.get("created_at") else "N/A"
        max_inc = f"+{r['max_daily_inc']:,}" if r.get("max_daily_inc") else "N/A"
        max_date = r.get("max_inc_date", "—")
        desc_raw = r.get("description") or "暂无描述"
        desc = translate_desc(desc_raw).replace("|", "\\|")[:200]

        lines.append(f"| {name} | {stars} | {forks} | {created} | {max_inc} | {max_date} | {desc} |")

    lines.extend([
        "",
        "---",
        "",
        f"*🤖 自动生成于 {today} · 数据来源 [GitHub API](https://api.github.com) · 项目地址 [ai-star-tracker](https://github.com/jerizhangWeb3th/ai-star-tracker)*",
    ])

    return "\n".join(lines) + "\n"


def git_commit_and_push():
    """提交并推送到 GitHub（SSH，绕过 DNS 污染）。"""
    try:
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "remote", "set-url", "origin",
             "git@github.com:jerizhangWeb3th/ai-star-tracker.git"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "add", "README.md",
             "data/history.json", "data/top20_history.json", "data/translations.json"],
            check=True,
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "commit", "-m", f"📊 每日更新 {today}"],
            check=True,
        )
        # 使用 SSH config 文件指定真实 IP（绕过 DNS 污染）
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "push", "origin", "main"],
            check=True, timeout=60,
            env={**os.environ, "GIT_SSH_COMMAND": "ssh -F /tmp/gh_ssh_config"},
        )
        print("✅ 已提交并推送到 GitHub")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Git 操作失败：{e}", file=sys.stderr)


def main():
    no_commit = "--no-commit" in sys.argv

    ensure_ssh_config()  # 确保 SSH 配置可用

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

    # 4. 取今日 Top 20
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    top20 = [
        {**r, "date": today}
        for r in gainers[:TOP_N]
        if r.get("star_increase") is not None and r["star_increase"] > 0
    ]
    # 如果正增长不够 20 个，用新增项目补足
    if len(top20) < TOP_N:
        new_repos = [r for r in gainers if r.get("star_increase") is None]
        for r in new_repos:
            if len(top20) >= TOP_N:
                break
            top20.append({**r, "date": today, "star_increase": 0})
    print(f"\n   ✅ 今日 Top {len(top20)}：")
    for i, r in enumerate(top20[:10], 1):
        inc = r.get("star_increase")
        inc_str = f"+{inc:,}" if inc else "NEW"
        print(f"      {i:>2}. {inc_str:>10}  {r['full_name']}")

    # 5. 更新历史汇总
    print("\n📊 更新历史汇总…")
    top20_history = load_top20_history()
    top20_history = update_top20_history(top20, top20_history)
    save_top20_history(top20_history)
    print(f"   ✅ 历史累计 {len(top20_history)} 个项目")

    # 6. 生成报告
    print("\n📝 生成 Markdown 报告…")
    report = generate_report(top20, top20_history)
    REPORT_FILE.write_text(report)
    print(f"   ✅ 已写入 {REPORT_FILE}")

    # 7. 保存历史
    print("\n💾 保存历史快照…")
    save_history(history)
    print(f"   ✅ 已保存 {len(history)} 个项目记录")

    # 8. 提交推送
    if not no_commit:
        print("\n🚀 提交并推送到 GitHub…")
        git_commit_and_push()

    print("\n" + "=" * 60)
    print("✅ 完成！")
    print(f"📄 报告地址: https://github.com/jerizhangWeb3th/ai-star-tracker")
    print("=" * 60)


if __name__ == "__main__":
    main()
