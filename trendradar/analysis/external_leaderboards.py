# coding=utf-8
"""
外部权威榜单（每周周报用）

目标：
- 不使用本项目内部的“标题命中统计”来生成热度榜（避免口径争议）
- 使用外部公开/权威数据源：
  - OpenRouter Rankings：模型使用量（token usage）+ share（%）
  - LMArena（HuggingFace Space 公布 CSV）：Arena-Hard-Auto 基准分（能力榜）
  - PyPI / npm：最近一周下载量（开发者采用度）

重要原则：
- 数据源不可用时：跳过该分榜，不阻塞周报主链路
"""

from __future__ import annotations

import csv
import io
import re
import time
import codecs
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml


_UA = "TrendRadar/weekly-leaderboard (+https://github.com/)"


@dataclass
class RankedItem:
    rank: int
    name: str
    metric: str
    url: str = ""
    extra: Optional[str] = None


def _http_get_text(url: str, timeout: int = 20) -> str:
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
    )
    resp.raise_for_status()
    return resp.text or ""


def _http_get_json(url: str, timeout: int = 20) -> Any:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": _UA, "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def load_external_leaderboards_config(
    project_root: str,
    rel_path: str = "config/external_leaderboards.yaml",
) -> Dict[str, Any]:
    path = Path(project_root) / rel_path
    if not path.exists():
        raise FileNotFoundError(f"外部榜单配置文件不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("external_leaderboards") or {}


def _parse_openrouter_top_models(page_text: str, top_n: int) -> List[RankedItem]:
    """
    解析 https://openrouter.ai/rankings 页面中的 Top Models 列表。

    典型结构（文本化后）：
    1.
    [Claude Sonnet 4.5](https://openrouter.ai/...)
    by[anthropic](...)
    766Btokens
    15%
    """
    items: List[RankedItem] = []

    # 优先尝试解析“展示文本”（某些环境会返回可读版本）
    pat = re.compile(
        r"(\d+)\.\s*"
        r"(?:!\[[^\]]*\]\([^)]+\)\s*)?"
        r"\[(?P<name>[^\]]+)\]\((?P<url>https?://openrouter\.ai/[^)]+)\)\s*"
        r"by\[(?P<author>[^\]]+)\]\([^)]+\)\s*"
        r"(?P<tokens>[\d.]+[KMBT]?)tokens\s*"
        r"(?P<share>[\d.]+%)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pat.finditer(page_text):
        rank = int(m.group(1))
        name = (m.group("name") or "").strip()
        url = (m.group("url") or "").strip()
        tokens = (m.group("tokens") or "").strip()
        share = (m.group("share") or "").strip()
        author = (m.group("author") or "").strip()
        if not name or not tokens or not share:
            continue
        metric = f"{tokens} tokens | {share}"
        extra = f"by {author}" if author else None
        items.append(RankedItem(rank=rank, name=name, metric=metric, url=url, extra=extra))
        if len(items) >= top_n:
            return items

    # 回退：从 Next.js RSC 注入的“模型统计数组”提取 request_count（仍是 OpenRouter 官方数据）
    # 形式：[{\"id\":\"...\",\"slug\":\"...\",\"name\":\"...\",\"author\":\"...\",\"request_count\":...}, ...]
    try:
        start_positions = [m.start() for m in re.finditer(r"\[\{\\\"id\\\":\\\"", page_text)]
        for start in start_positions:
            window = page_text[start : start + 2000]
            if "request_count" not in window:
                continue

            frag = page_text[start : start + 80000]
            level = 0
            end = None
            for i, ch in enumerate(frag):
                if ch == "[":
                    level += 1
                elif ch == "]":
                    level -= 1
                    if level == 0:
                        end = i + 1
                        break
            if not end:
                continue
            arr_esc = frag[:end]
            arr_json = arr_esc.replace("\\\"", "\"")
            models = __import__("json").loads(arr_json)
            if not isinstance(models, list) or not models:
                continue
            models = [m for m in models if isinstance(m, dict) and m.get("request_count")]
            models.sort(key=lambda x: int(x.get("request_count") or 0), reverse=True)
            ranked: List[RankedItem] = []
            for idx, mobj in enumerate(models[:top_n], 1):
                name = str(mobj.get("name") or "").strip()
                slug = str(mobj.get("slug") or "").strip()
                req = int(mobj.get("request_count") or 0)
                if not name or req <= 0:
                    continue
                url = f"https://openrouter.ai/{slug}" if slug else ""
                ranked.append(RankedItem(rank=idx, name=name, metric=f"{req:,} requests", url=url))
            return ranked
    except Exception:
        return []

    return []


def _parse_openrouter_top_apps(page_text: str, top_n: int) -> List[RankedItem]:
    """
    解析 https://openrouter.ai/rankings/apps 页面中的 Top Apps 列表。

    典型结构（文本化后）：
    1.
    [liteLLM](https://openrouter.ai/apps?url=...)
    Open-source library to simplify LLM calls
    66.9Btokens
    """
    items: List[RankedItem] = []
    # 从 “## Top Apps” 开始截断，避免前面的模型榜干扰
    idx = page_text.find("## Top Apps")
    text = page_text[idx:] if idx >= 0 else page_text

    pat = re.compile(
        r"(\d+)\.\s*"
        r"(?:!\[[^\]]*\]\([^)]+\)\s*)?"
        r"\[(?P<name>[^\]]+)\]\((?P<url>https?://openrouter\.ai/apps\?url=[^)]+)\)\s*"
        r"(?P<desc>.*?)\s*"
        r"(?P<tokens>[\d.]+[KMBT]?)tokens",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pat.finditer(text):
        rank = int(m.group(1))
        name = (m.group("name") or "").strip()
        url = (m.group("url") or "").strip()
        desc = (m.group("desc") or "").strip().replace("\n", " ")
        tokens = (m.group("tokens") or "").strip()
        if not name:
            continue
        items.append(RankedItem(rank=rank, name=name, metric=f"{tokens} tokens", url=url, extra=desc or None))
        if len(items) >= top_n:
            return items

    # 回退：解析 Next.js RSC 注入的 rankMap（包含 day/week/month 的 app total_tokens）
    try:
        push_re = re.compile(r'self\.__next_f\.push\(\[1,"(?P<payload>(?:\\.|[^"])*)"\]\)')
        payload_with_rankmap = None
        for mm in push_re.finditer(page_text):
            payload = mm.group("payload")
            if "rankMap" in payload:
                payload_with_rankmap = payload
                break
        if not payload_with_rankmap:
            return []

        decoded = codecs.decode(payload_with_rankmap, "unicode_escape")
        pos_candidates = [p for p in (decoded.find("["), decoded.find("{")) if p != -1]
        if not pos_candidates:
            return []
        js = decoded[min(pos_candidates) :]
        data = __import__("json").loads(js)

        rankmap = None
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if "rankMap" in cur and isinstance(cur["rankMap"], dict):
                    rankmap = cur["rankMap"]
                    break
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)

        if not rankmap:
            return []
        period = "week" if "week" in rankmap else ("day" if "day" in rankmap else "month")
        arr = rankmap.get(period) or []
        if not isinstance(arr, list) or not arr:
            return []

        def _fmt_tokens(n: int) -> str:
            for unit, div in (("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3)):
                if n >= div:
                    return f"{n/div:.1f}{unit} tokens"
            return f"{n} tokens"

        ranked: List[RankedItem] = []
        for i, entry in enumerate(arr[:top_n], 1):
            if not isinstance(entry, dict):
                continue
            app = entry.get("app") or {}
            title = str(app.get("title") or "").strip()
            if not title:
                continue
            total_tokens_str = str(entry.get("total_tokens") or "0")
            try:
                total_tokens = int(total_tokens_str)
            except Exception:
                total_tokens = 0
            url = str(app.get("origin_url") or app.get("main_url") or "").strip()
            ranked.append(RankedItem(rank=i, name=title, metric=_fmt_tokens(total_tokens), url=url, extra=app.get("description") or None))
        return ranked
    except Exception:
        return []


def fetch_openrouter_rankings(
    *,
    url_models: str,
    url_apps: str,
    include_apps: bool,
    top_n: int,
) -> Tuple[List[RankedItem], List[RankedItem]]:
    models: List[RankedItem] = []
    apps: List[RankedItem] = []

    # 同一个页面里通常包含模型统计（request_count）与 apps rankMap（total_tokens）。
    try:
        text = _http_get_text(url_models)
        models = _parse_openrouter_top_models(text, top_n=top_n)
        # apps 的 rankMap 也在 rankings 页里，优先用同一份 HTML，避免重复请求
        if include_apps:
            apps = _parse_openrouter_top_apps(text, top_n=top_n)
    except Exception:
        models = []
        apps = []

    # 若需要 apps 且未取到，再尝试 apps 页面
    if include_apps and not apps:
        try:
            text = _http_get_text(url_apps)
            apps = _parse_openrouter_top_apps(text, top_n=top_n)
        except Exception:
            apps = []

    return models, apps


def fetch_lmarena_arena_hard_auto(
    *,
    csv_url: str,
    top_n: int,
) -> Tuple[List[RankedItem], Optional[str]]:
    """
    返回 (榜单, date_str)
    """
    try:
        csv_text = _http_get_text(csv_url)
    except Exception:
        return [], None

    rows: List[Dict[str, str]] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if not row:
            continue
        rows.append({k: (v or "").strip() for k, v in row.items()})

    if not rows:
        return [], None

    # 尝试提取 date（文件里通常相同）
    date_str = None
    for r in rows:
        if r.get("date"):
            date_str = r.get("date")
            break

    def _score(row: Dict[str, str]) -> float:
        try:
            return float(row.get("score", "") or 0)
        except Exception:
            return 0.0

    rows.sort(key=lambda r: _score(r), reverse=True)
    items: List[RankedItem] = []
    for i, r in enumerate(rows[:top_n], 1):
        name = r.get("model", "") or ""
        score = r.get("score", "") or ""
        ci = r.get("CI", "") or ""
        metric = f"score {score}"
        extra = f"CI {ci}" if ci else None
        items.append(RankedItem(rank=i, name=name, metric=metric, extra=extra))
    return items, date_str


def fetch_pypi_downloads_last_week(package: str, timeout: int = 20) -> Optional[int]:
    """
    pypistats: https://pypistats.org/api/packages/<package>/recent?period=week
    """
    url = f"https://pypistats.org/api/packages/{package}/recent?period=week"
    backoff = 1.0
    for _ in range(3):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": _UA, "Accept": "application/json"})
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(10, int(retry_after)))
                else:
                    time.sleep(min(10, backoff))
                    backoff *= 2
                continue
            resp.raise_for_status()
            data = resp.json()
            val = (data or {}).get("data", {}).get("last_week")
            if isinstance(val, int):
                return val
            if isinstance(val, float):
                return int(val)
            return None
        except Exception:
            time.sleep(min(5, backoff))
            backoff *= 2
            continue
    return None


def fetch_npm_downloads_last_week(package: str, timeout: int = 20) -> Optional[int]:
    """
    npm downloads API:
    https://api.npmjs.org/downloads/point/last-week/<package>
    """
    try:
        data = _http_get_json(f"https://api.npmjs.org/downloads/point/last-week/{package}", timeout=timeout)
        val = (data or {}).get("downloads")
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return int(val)
        return None
    except Exception:
        return None


def _render_ranked_list(items: List[RankedItem]) -> List[str]:
    lines: List[str] = []
    for it in items:
        if it.url:
            lines.append(f"{it.rank}. **[{it.name}]({it.url})** — {it.metric}" + (f"（{it.extra}）" if it.extra else ""))
        else:
            lines.append(f"{it.rank}. **{it.name}** — {it.metric}" + (f"（{it.extra}）" if it.extra else ""))
    return lines


def render_weekly_external_leaderboards_markdown(
    *,
    project_root: str,
    top_n: int = 10,
) -> str:
    cfg = load_external_leaderboards_config(project_root)
    top_n = max(1, int(cfg.get("top_n", top_n) or top_n))

    lines: List[str] = []
    lines.append("## 📈 AI模型/工具热度排行榜（外部权威口径）")
    lines.append("")
    lines.append("> 说明：本区块来自外部公开数据源（OpenRouter / LMArena / PyPI / npm），用于“行业热度/采用度”参考；若某源不可用将自动跳过，不影响周报生成。")
    lines.append("")

    # ===== 模型榜：OpenRouter =====
    models_cfg = (cfg.get("models") or {}) if isinstance(cfg, dict) else {}
    openrouter_cfg = (models_cfg.get("openrouter") or {}) if isinstance(models_cfg, dict) else {}
    if isinstance(openrouter_cfg, dict) and openrouter_cfg.get("enabled", False):
        url_models = str(openrouter_cfg.get("url_models", "https://openrouter.ai/rankings"))
        url_apps = str(openrouter_cfg.get("url_apps", "https://openrouter.ai/rankings/apps"))
        include_apps = bool(openrouter_cfg.get("include_apps", False))

        top_models, top_apps = fetch_openrouter_rankings(
            url_models=url_models,
            url_apps=url_apps,
            include_apps=include_apps,
            top_n=top_n,
        )

        if top_models:
            lines.append("### OpenRouter：模型热度榜（requests）")
            lines.append("")
            lines.append("> 口径：从 OpenRouter 排行页注入的模型统计中提取 request_count（请求量）。")
            lines.append("")
            lines.extend(_render_ranked_list(top_models))
            lines.append("")

        if include_apps and top_apps:
            lines.append("### OpenRouter：Top Apps（opt-in 使用追踪）")
            lines.append("")
            lines.append("> 口径：OpenRouter 公布的 opt-in 应用 total_tokens（不是全网；仅统计选择上报的应用）。")
            lines.append("")
            lines.extend(_render_ranked_list(top_apps))
            lines.append("")

    # ===== 模型榜：LMArena（Arena-Hard-Auto） =====
    lmarena_cfg = (models_cfg.get("lmarena") or {}) if isinstance(models_cfg, dict) else {}
    if isinstance(lmarena_cfg, dict) and lmarena_cfg.get("enabled", False):
        csv_url = str(lmarena_cfg.get("arena_hard_auto_csv", "") or "").strip()
        if csv_url:
            items, date_str = fetch_lmarena_arena_hard_auto(csv_url=csv_url, top_n=top_n)
            if items:
                lines.append("### LMArena：Arena-Hard-Auto（能力榜）")
                lines.append("")
                # 避免误读：明确不是 Elo（人类投票）
                if date_str:
                    lines.append(f"> 口径：Arena-Hard-Auto 分数（date={date_str}）。注意：这不是人类投票 Elo，而是 LMArena 发布的基准评分。")
                else:
                    lines.append("> 口径：Arena-Hard-Auto 分数。注意：这不是人类投票 Elo，而是 LMArena 发布的基准评分。")
                lines.append("")
                lines.extend(_render_ranked_list(items))
                lines.append("")

    # ===== 工具榜：PyPI / npm =====
    tools_cfg = (cfg.get("tools") or {}) if isinstance(cfg, dict) else {}

    # PyPI
    pypi_cfg = (tools_cfg.get("pypi") or {}) if isinstance(tools_cfg, dict) else {}
    if isinstance(pypi_cfg, dict) and pypi_cfg.get("enabled", False):
        pkgs = pypi_cfg.get("packages") or []
        results: List[Tuple[str, str, int]] = []  # (name, pkg, downloads)
        failed: List[str] = []
        if isinstance(pkgs, list):
            for item in pkgs:
                if not isinstance(item, dict):
                    continue
                pkg = str(item.get("package", "") or "").strip()
                name = str(item.get("name", "") or pkg).strip()
                if not pkg:
                    continue
                downloads = fetch_pypi_downloads_last_week(pkg)
                if downloads is None:
                    failed.append(pkg)
                    continue
                results.append((name, pkg, downloads))
        if results:
            results.sort(key=lambda x: x[2], reverse=True)
            lines.append("### 工具：PyPI 下载量（last_week）")
            lines.append("")
            lines.append("> 口径：pypistats 最近一周下载量（last_week）。")
            if failed:
                lines.append(f"> 获取失败已跳过：{', '.join(failed)}")
            lines.append("")
            for idx, (name, pkg, downloads) in enumerate(results[:top_n], 1):
                lines.append(f"{idx}. **{name}** (`{pkg}`) — {downloads:,} downloads")
            lines.append("")

    # npm
    npm_cfg = (tools_cfg.get("npm") or {}) if isinstance(tools_cfg, dict) else {}
    if isinstance(npm_cfg, dict) and npm_cfg.get("enabled", False):
        pkgs = npm_cfg.get("packages") or []
        results = []  # (name, pkg, downloads)
        failed: List[str] = []
        if isinstance(pkgs, list):
            for item in pkgs:
                if not isinstance(item, dict):
                    continue
                pkg = str(item.get("package", "") or "").strip()
                name = str(item.get("name", "") or pkg).strip()
                if not pkg:
                    continue
                downloads = fetch_npm_downloads_last_week(pkg)
                if downloads is None:
                    failed.append(pkg)
                    continue
                results.append((name, pkg, downloads))
        if results:
            results.sort(key=lambda x: x[2], reverse=True)
            lines.append("### 工具：npm 下载量（last-week）")
            lines.append("")
            lines.append("> 口径：npm downloads API `last-week` 下载量。")
            if failed:
                lines.append(f"> 获取失败已跳过：{', '.join(failed)}")
            lines.append("")
            for idx, (name, pkg, downloads) in enumerate(results[:top_n], 1):
                lines.append(f"{idx}. **{name}** (`{pkg}`) — {downloads:,} downloads")
            lines.append("")

    # 如果没有任何分榜成功，返回空字符串（不污染周报）
    has_payload = any(line.startswith("### ") for line in lines)
    if not has_payload:
        return ""
    return "\n".join(lines).strip()

