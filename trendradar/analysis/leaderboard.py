# coding=utf-8
"""
每周热度排行榜（仅热榜 news 数据）

目标：
- 用可编辑的配置（config/leaderboard_entities.yaml）维护“模型/框架/工具”等实体词表
- 遍历最近 N 天 output/news/*.db 的标题，按实体匹配统计：
  - 提及次数（命中标题条数）
  - 热度分（复用现有 calculate_news_weight 的权重计算，跨天累计）
  - 样本标题（按热度分选取 1-2 条）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from mcp_server.services.parser_service import ParserService
from trendradar.core.analyzer import calculate_news_weight


@dataclass
class MatchPattern:
    raw: str
    is_regex: bool
    regex: Optional[re.Pattern] = None
    substring: Optional[str] = None

    def matches(self, title: str) -> bool:
        if not title:
            return False
        if self.is_regex and self.regex is not None:
            return bool(self.regex.search(title))
        if self.substring is not None:
            return self.substring in title.lower()
        return False


@dataclass
class LeaderboardItemConfig:
    id: str
    name: str
    patterns: List[MatchPattern]


@dataclass
class LeaderboardCategoryConfig:
    id: str
    name: str
    items: List[LeaderboardItemConfig]


@dataclass
class SampleHit:
    date: str
    platform: str
    title: str
    weight: float


@dataclass
class ItemStats:
    item_id: str
    item_name: str
    mentions: int = 0
    hot_score: float = 0.0
    samples: List[SampleHit] = field(default_factory=list)
    _seen_keys: set = field(default_factory=set, repr=False)

    def add_hit(self, key: str, hit: SampleHit) -> None:
        if key in self._seen_keys:
            return
        self._seen_keys.add(key)
        self.mentions += 1
        self.hot_score += hit.weight
        self.samples.append(hit)


def _parse_regex_token(token: str) -> Optional[re.Pattern]:
    """
    解析 /.../flags 形式的正则 token。
    目前统一使用 IGNORECASE（即使 flags 未提供）。
    """
    token = token.strip()
    if not (token.startswith("/") and token.count("/") >= 2):
        return None

    # 取第一个 / 和最后一个 / 之间的内容作为 pattern；末尾 flags 忽略
    last_slash = token.rfind("/")
    if last_slash <= 0:
        return None
    pattern_str = token[1:last_slash]
    if not pattern_str:
        return None
    try:
        return re.compile(pattern_str, re.IGNORECASE)
    except re.error:
        return None


def _compile_patterns(patterns: Iterable[str]) -> List[MatchPattern]:
    compiled: List[MatchPattern] = []
    for p in patterns:
        raw = (p or "").strip()
        if not raw:
            continue
        regex = _parse_regex_token(raw)
        if regex is not None:
            compiled.append(MatchPattern(raw=raw, is_regex=True, regex=regex))
        else:
            compiled.append(MatchPattern(raw=raw, is_regex=False, substring=raw.lower()))
    return compiled


def load_leaderboard_config(project_root: str, rel_path: str = "config/leaderboard_entities.yaml") -> List[LeaderboardCategoryConfig]:
    path = Path(project_root) / rel_path
    if not path.exists():
        raise FileNotFoundError(f"leaderboard 配置文件不存在: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    categories_raw = data.get("categories") or []
    categories: List[LeaderboardCategoryConfig] = []

    for cat in categories_raw:
        if not isinstance(cat, dict):
            continue
        cat_id = str(cat.get("id", "")).strip()
        cat_name = str(cat.get("name", "")).strip()
        if not cat_id or not cat_name:
            continue
        items: List[LeaderboardItemConfig] = []
        for it in (cat.get("items") or []):
            if not isinstance(it, dict):
                continue
            it_id = str(it.get("id", "")).strip()
            it_name = str(it.get("name", "")).strip()
            patterns_raw = it.get("patterns") or []
            if not it_id or not it_name or not isinstance(patterns_raw, list):
                continue
            compiled = _compile_patterns([str(x) for x in patterns_raw if x is not None])
            if not compiled:
                continue
            items.append(LeaderboardItemConfig(id=it_id, name=it_name, patterns=compiled))
        if items:
            categories.append(LeaderboardCategoryConfig(id=cat_id, name=cat_name, items=items))

    return categories


def compute_weekly_leaderboard(
    *,
    project_root: str,
    end_time: datetime,
    days: int = 7,
    top_n: int = 10,
    rank_threshold: int = 5,
    weight_config: Optional[Dict[str, float]] = None,
    rel_entities_path: str = "config/leaderboard_entities.yaml",
) -> List[Tuple[LeaderboardCategoryConfig, List[ItemStats]]]:
    """
    计算最近 N 天的热度排行榜。

    Returns:
        [(category_config, top_item_stats_list), ...]
    """
    if days <= 0:
        return []
    top_n = max(1, int(top_n))

    if not weight_config:
        weight_config = {"RANK_WEIGHT": 0.6, "FREQUENCY_WEIGHT": 0.3, "HOTNESS_WEIGHT": 0.1}

    categories = load_leaderboard_config(project_root, rel_entities_path)
    if not categories:
        return []

    parser = ParserService(project_root=project_root)

    # 准备 stats 容器
    stats_map: Dict[Tuple[str, str], ItemStats] = {}  # (cat_id, item_id) -> stats
    for cat in categories:
        for it in cat.items:
            stats_map[(cat.id, it.id)] = ItemStats(item_id=it.id, item_name=it.name)

    end_date = end_time.date()
    start_date = end_date - timedelta(days=days - 1)

    # 逐日读取 news db 并统计
    current = start_date
    while current <= end_date:
        date_dt = datetime.combine(current, datetime.min.time())
        date_str = current.strftime("%Y-%m-%d")
        try:
            all_titles, id_to_name, _ = parser.read_all_titles_for_date(date=date_dt, db_type="news")
        except Exception:
            current += timedelta(days=1)
            continue

        for platform_id, titles in all_titles.items():
            platform_name = id_to_name.get(platform_id, platform_id)
            for title, info in titles.items():
                if not title:
                    continue
                ranks = info.get("ranks", []) or []
                count = info.get("count", len(ranks) if ranks else 1) or 1
                weight = calculate_news_weight(
                    {"ranks": ranks, "count": count},
                    rank_threshold,
                    weight_config,
                )

                title_lower = title.lower()

                # 每条标题按配置逐项匹配
                for cat in categories:
                    for it in cat.items:
                        if not any(p.matches(title_lower) for p in it.patterns):
                            continue
                        key = f"{date_str}::{platform_id}::{title}"
                        stats_map[(cat.id, it.id)].add_hit(
                            key=key,
                            hit=SampleHit(
                                date=date_str,
                                platform=platform_name,
                                title=title,
                                weight=weight,
                            ),
                        )

        current += timedelta(days=1)

    results: List[Tuple[LeaderboardCategoryConfig, List[ItemStats]]] = []
    for cat in categories:
        items_stats = [stats_map[(cat.id, it.id)] for it in cat.items]

        # 过滤无命中
        items_stats = [s for s in items_stats if s.mentions > 0]
        if not items_stats:
            continue

        # 排序：提及次数 > 热度分 > 名称
        items_stats.sort(key=lambda s: (-s.mentions, -s.hot_score, s.item_name))
        items_stats = items_stats[:top_n]

        # 样本：按权重分降序选 2 条（权重相同按日期/标题保证稳定）
        for s in items_stats:
            s.samples.sort(key=lambda x: (-x.weight, x.date, x.platform, x.title))
            s.samples = s.samples[:2]

        results.append((cat, items_stats))

    return results


def render_weekly_leaderboard_markdown(
    *,
    project_root: str,
    end_time: datetime,
    top_n: int = 10,
    rank_threshold: int = 5,
    weight_config: Optional[Dict[str, float]] = None,
    rel_entities_path: str = "config/leaderboard_entities.yaml",
) -> str:
    results = compute_weekly_leaderboard(
        project_root=project_root,
        end_time=end_time,
        days=7,
        top_n=top_n,
        rank_threshold=rank_threshold,
        weight_config=weight_config,
        rel_entities_path=rel_entities_path,
    )
    if not results:
        return ""

    lines: List[str] = []
    lines.append("## 📈 AI模型/工具热度排行榜（热榜）")
    lines.append("")
    lines.append("> 统计口径：最近7天热榜标题命中次数 + 热度分（按排名/频次/高位占比加权累计）。")
    lines.append("")

    for cat, items_stats in results:
        lines.append(f"### {cat.name}")
        lines.append("")
        for idx, s in enumerate(items_stats, 1):
            lines.append(f"{idx}. **{s.item_name}** — 提及 {s.mentions} | 热度 {s.hot_score:.1f}")
            for sample in s.samples:
                lines.append(f"   - [{sample.platform}] {sample.title} ({sample.date})")
        lines.append("")

    return "\n".join(lines).strip()

