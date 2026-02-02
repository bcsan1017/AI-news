# coding=utf-8
"""
AI 质量闸门（推送前快速复筛）

目标：
- 在 frequency_words.txt 的“关键词粗筛”之后、推送之前，
  使用轻量模型（默认 gemini-3-flash-preview）快速判断每条内容是否值得推送，
  过滤明显无关/低价值内容（如纯社会八卦、娱乐、泛政治等），降低噪音。

设计原则：
- 默认关闭：不增加成本与延迟
- 失败降级：模型不可用/超时/解析失败时，直接跳过过滤（不影响原有推送）
- 快速批量：尽量用一次请求对多条候选进行判定
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class QualityGateStats:
    """质量闸门统计信息（仅用于日志）"""

    total_items: int = 0
    evaluated_items: int = 0
    kept_items: int = 0
    dropped_items: int = 0
    error: str = ""


def _safe_json_extract(text: str) -> Optional[str]:
    """
    尽力从模型输出中提取 JSON（支持模型返回 ```json ... ``` 包裹、或夹杂解释文本）。
    返回 JSON 字符串（以 '[' 或 '{' 开头），失败返回 None。
    """
    if not text:
        return None
    s = text.strip()
    # 去掉可能的 code fence
    if s.startswith("```"):
        # 兼容 ```json
        s = s.strip("`").strip()
        # 去掉首行可能的语言标记
        lines = s.splitlines()
        if lines and lines[0].strip().lower() in ("json", "javascript"):
            s = "\n".join(lines[1:]).strip()

    # 优先寻找数组（我们期望是 list）
    l = s.find("[")
    r = s.rfind("]")
    if l != -1 and r != -1 and r > l:
        return s[l : r + 1].strip()

    # 兜底寻找对象
    l = s.find("{")
    r = s.rfind("}")
    if l != -1 and r != -1 and r > l:
        return s[l : r + 1].strip()

    return None


class AIQualityGate:
    """
    推送前质量复筛器

    输入：report_data（热榜 stats/new_titles），以及 rss_items/rss_new_items（若启用合并推送）。
    输出：过滤后的数据结构（保持字段结构不变，但会裁剪 titles，并更新 count/total_new_count）。
    """

    def __init__(self, ai_config: Dict[str, Any], gate_config: Dict[str, Any]):
        self.ai_config = dict(ai_config or {})
        self.gate_config = dict(gate_config or {})

        self.enabled = bool(self.gate_config.get("ENABLED", False))
        self.model = (self.gate_config.get("MODEL") or "gemini-3-flash-preview").strip()
        self.min_score = int(self.gate_config.get("MIN_SCORE", 60) or 60)
        # 评估上限：
        # - >0：最多评估前 N 条（剩余默认保留）
        # - =0：尽量全量评估（更全面，但更耗时/更费钱）
        self.max_items = int(self.gate_config.get("MAX_ITEMS", 30) or 30)
        # 每次请求评估条数（用于分批，避免一次输入过长导致失败）
        self.batch_size = int(self.gate_config.get("BATCH_SIZE", 40) or 40)
        self.timeout = int(self.gate_config.get("TIMEOUT", 30) or 30)
        self.max_tokens = int(self.gate_config.get("MAX_TOKENS", 900) or 900)
        self.reasoning_effort = (self.gate_config.get("REASONING_EFFORT") or "low").strip()
        self.debug = bool(self.gate_config.get("DEBUG", False))

        # 更鲁棒：读取正文片段后再做复筛/总结（失败自动降级到标题）
        self.use_content = bool(self.gate_config.get("USE_CONTENT", False))
        self.content_fetch_timeout = int(self.gate_config.get("CONTENT_FETCH_TIMEOUT", 10) or 10)
        self.content_fetch_concurrency = int(self.gate_config.get("CONTENT_FETCH_CONCURRENCY", 6) or 6)
        self.max_content_chars = int(self.gate_config.get("MAX_CONTENT_CHARS", 4000) or 4000)

        # 每条新闻下方的“精华更新点”提示词（允许用户在 config/ 中自行修改）
        self.brief_prompt_file = (self.gate_config.get("BRIEF_PROMPT_FILE") or "quality_gate_brief_prompt.txt").strip()
        self.brief_prompt_text = self._load_brief_prompt(self.brief_prompt_file)

    def _load_brief_prompt(self, prompt_file: str) -> str:
        """从 config/ 目录加载 brief 提示词（不存在则返回空字符串）。"""
        try:
            config_dir = Path(__file__).parent.parent.parent / "config"
            p = config_dir / (prompt_file or "")
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return ""

    def filter_before_send(
        self,
        report_data: Dict[str, Any],
        mode: str,
        rss_items: Optional[List[Dict[str, Any]]] = None,
        rss_new_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]], QualityGateStats]:
        stats = QualityGateStats()
        if not self.enabled:
            return report_data, rss_items, rss_new_items, stats

        # 没有 API Key 就不做复筛（避免误伤）
        api_key = (self.ai_config.get("API_KEY") or "").strip()
        env_key = ""  # AIClient 内部也会读 env，这里只做一个提示性检查
        if not api_key and not env_key:
            stats.error = "未配置 AI_API_KEY，跳过质量复筛"
            return report_data, rss_items, rss_new_items, stats

        # 收集候选条目（限制数量，优先保留更靠前的内容）
        items, pointers = self._collect_candidates(report_data, rss_items, rss_new_items)
        stats.total_items = len(items)
        if not items:
            return report_data, rss_items, rss_new_items, stats

        # 限制评估数量（其余默认保留，避免“误删且不可解释”）
        eval_items = items[: self.max_items] if self.max_items > 0 else items
        stats.evaluated_items = len(eval_items)

        try:
            decisions = self._judge_in_batches(eval_items, mode=mode)
        except Exception as e:
            stats.error = f"{type(e).__name__}: {e}"
            return report_data, rss_items, rss_new_items, stats

        # 应用结果（未被评估到的默认保留）
        keep_ids = set()
        for it in items:
            it_id = it["id"]
            d = decisions.get(it_id)
            if d is None:
                keep_ids.add(it_id)
                continue
            keep = bool(d.get("keep", False))
            score = int(d.get("score", 0) or 0)
            if keep and score >= self.min_score:
                keep_ids.add(it_id)

        filtered_report_data, filtered_rss_items, filtered_rss_new_items = self._apply_kept_ids(
            report_data, rss_items, rss_new_items, keep_ids, pointers, decisions
        )

        stats.kept_items = len(keep_ids)
        stats.dropped_items = max(0, stats.total_items - stats.kept_items)
        return filtered_report_data, filtered_rss_items, filtered_rss_new_items, stats

    def _collect_candidates(
        self,
        report_data: Dict[str, Any],
        rss_items: Optional[List[Dict[str, Any]]],
        rss_new_items: Optional[List[Dict[str, Any]]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """
        返回：
        - items: [{id, title, source_name, group, section}]
        - pointers: id -> {section, index_path} 用于回写过滤
        """
        items: List[Dict[str, Any]] = []
        pointers: Dict[str, Dict[str, Any]] = {}

        # 1) 热榜 stats
        for gi, g in enumerate(report_data.get("stats", []) or []):
            group = (g.get("word") or "").strip()
            titles = g.get("titles", []) or []
            for ti, t in enumerate(titles):
                tid = f"hot_stats:{gi}:{ti}"
                title = (t.get("title") or "").strip()
                if not title:
                    continue
                link_url = (t.get("mobileUrl") or t.get("mobile_url") or t.get("url") or "").strip()
                items.append(
                    {
                        "id": tid,
                        "title": title,
                        "source_name": (t.get("source_name") or "").strip(),
                        "group": group,
                        "section": "hot_stats",
                        "url": link_url,
                    }
                )
                pointers[tid] = {"section": "hot_stats", "gi": gi, "ti": ti}

        # 2) 热榜 new_titles（daily/current 才有）
        for si, s in enumerate(report_data.get("new_titles", []) or []):
            titles = s.get("titles", []) or []
            for ti, t in enumerate(titles):
                tid = f"hot_new:{si}:{ti}"
                title = (t.get("title") or "").strip()
                if not title:
                    continue
                link_url = (t.get("mobileUrl") or t.get("mobile_url") or t.get("url") or "").strip()
                items.append(
                    {
                        "id": tid,
                        "title": title,
                        "source_name": (t.get("source_name") or "").strip(),
                        "group": "",  # new_titles 不一定有 group
                        "section": "hot_new",
                        "url": link_url,
                    }
                )
                pointers[tid] = {"section": "hot_new", "si": si, "ti": ti}

        # 3) RSS stats
        for gi, g in enumerate(rss_items or []):
            group = (g.get("word") or "").strip()
            titles = g.get("titles", []) or []
            for ti, t in enumerate(titles):
                tid = f"rss_stats:{gi}:{ti}"
                title = (t.get("title") or "").strip()
                if not title:
                    continue
                link_url = (t.get("mobileUrl") or t.get("mobile_url") or t.get("url") or "").strip()
                items.append(
                    {
                        "id": tid,
                        "title": title,
                        "source_name": (t.get("source_name") or "").strip(),
                        "group": group,
                        "section": "rss_stats",
                        "url": link_url,
                    }
                )
                pointers[tid] = {"section": "rss_stats", "gi": gi, "ti": ti}

        # 4) RSS new
        for gi, g in enumerate(rss_new_items or []):
            group = (g.get("word") or "").strip()
            titles = g.get("titles", []) or []
            for ti, t in enumerate(titles):
                tid = f"rss_new:{gi}:{ti}"
                title = (t.get("title") or "").strip()
                if not title:
                    continue
                link_url = (t.get("mobileUrl") or t.get("mobile_url") or t.get("url") or "").strip()
                items.append(
                    {
                        "id": tid,
                        "title": title,
                        "source_name": (t.get("source_name") or "").strip(),
                        "group": group,
                        "section": "rss_new",
                        "url": link_url,
                    }
                )
                pointers[tid] = {"section": "rss_new", "gi": gi, "ti": ti}

        return items, pointers

    def _judge_in_batches(self, items: List[Dict[str, Any]], mode: str) -> Dict[str, Dict[str, Any]]:
        """
        分批评估（更稳、更全面）。
        注意：若 batch_size <= 0，则退化为单批。
        """
        if not items:
            return {}

        batch_size = self.batch_size if self.batch_size and self.batch_size > 0 else len(items)
        decisions: Dict[str, Dict[str, Any]] = {}

        # 分批调用：避免单次输入过长；每批失败会抛异常由上层统一降级
        for start in range(0, len(items), batch_size):
            chunk = items[start : start + batch_size]
            chunk_decisions = self._judge_batch(chunk, mode=mode)
            if chunk_decisions:
                decisions.update(chunk_decisions)

        return decisions

    def _judge_batch(self, items: List[Dict[str, Any]], mode: str) -> Dict[str, Dict[str, Any]]:
        """
        返回：id -> {keep: bool, score: int, reason?: str, brief?: str}
        """
        # 延迟导入，避免在未启用时引入 litellm 依赖
        from trendradar.ai.client import AIClient

        cfg = dict(self.ai_config)
        cfg["MODEL"] = self.model or cfg.get("MODEL", "gemini-3-flash-preview")

        client = AIClient(cfg)

        # 说明：这里用中文提示词，输出严格 JSON，便于解析
        system = (
            "你是一个“信息推送质量闸门”。你的任务是对候选新闻标题做快速复筛，只保留高信噪比内容。\n"
            "\n"
            "【硬性规则（必须同时满足其一）】\n"
            "A) 明确与 AI 技术/产品相关（模型发布/评测/Agent 工具链/多模态/推理/安全/端侧推理/芯片）。\n"
            "B) 明确与可穿戴/XR 产品执行相关（量产/供应链/合规/渠道/定价），但必须出现可穿戴/XR 相关上下文（眼镜/耳机/手表/戒指/AR/VR/XR/具体品牌）。\n"
            "\n"
            "【必须过滤】\n"
            "- 纯社会新闻、娱乐八卦、泛财经金价/股价、泛政治外交、明星事件。\n"
            "- 仅因“成本/渠道/价格”等泛词命中的内容（没有 AI 或可穿戴上下文）。\n"
            "- 折扣促销/消费导购（除非是关键新品发布/监管/量产事件且与可穿戴/XR 强相关）。\n"
            "\n"
            "【评分标准】\n"
            "- 0-40：明显无关/低价值（应 drop）\n"
            "- 41-70：弱相关但不值得打扰（默认 drop）\n"
            "- 71-85：相关且有价值（可 keep）\n"
            "- 86-100：强信号（优先 keep）\n"
        )

        brief_rules = (self.brief_prompt_text or "").strip()
        if brief_rules:
            system += "\n\n【精华更新点（brief）输出要求】\n" + brief_rules + "\n"
        else:
            system += (
                "\n\n【精华更新点（brief）输出要求】\n"
                "- 输出一句中文（最多 40 字），强调“发生了什么变化/意味着什么”。\n"
                "- 不要复述标题；信息不足时要保守，不要编造。\n"
            )

        # 可选：抓取正文片段，提高鲁棒性（失败自动降级）
        if self.use_content:
            try:
                from trendradar.utils.content_fetch import fetch_url_text
            except Exception:
                fetch_url_text = None
        else:
            fetch_url_text = None

        url_to_content: Dict[str, str] = {}
        if fetch_url_text:
            urls = [((it.get("url") or "").strip()) for it in items]
            urls = [u for u in urls if u]

            # 并发抓取，避免 40 条顺序抓取导致超时
            workers = self.content_fetch_concurrency if self.content_fetch_concurrency and self.content_fetch_concurrency > 0 else 6
            workers = min(max(workers, 1), 16)
            if urls and workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {
                        ex.submit(fetch_url_text, u, self.content_fetch_timeout, self.max_content_chars): u
                        for u in urls
                    }
                    for fut in as_completed(futs):
                        u = futs[fut]
                        try:
                            txt = fut.result()
                        except Exception:
                            txt = None
                        if txt:
                            url_to_content[u] = txt
            else:
                for u in urls:
                    txt = fetch_url_text(u, timeout=self.content_fetch_timeout, max_chars=self.max_content_chars)
                    if txt:
                        url_to_content[u] = txt

        enriched_payload = []
        for it in items:
            url = (it.get("url") or "").strip()
            enriched_payload.append(
                {
                    "id": it["id"],
                    "title": it["title"],
                    "source": it["source_name"],
                    "group": it["group"],
                    "url": url,
                    "content": url_to_content.get(url, "") if url else "",
                }
            )

        # 构造批量输入
        # 注意：group 可能包含“可穿戴_量产与合规信号”等，用于识别误命中
        user = (
            "请对以下候选逐条判定是否值得推送。\n"
            f"- 推送模式：{mode}\n\n"
            "输出必须是 JSON 数组，数组中每个对象包含：\n"
            '- "id": 与输入一致\n'
            '- "keep": true/false（是否保留推送）\n'
            '- "score": 0-100（价值/相关性评分，越高越值得推送）\n'
            '- "reason": 简短中文原因（不超过 20 字）\n\n'
            '- "brief": 一句中文精华更新点（最多 40 字）\n\n'
            "只输出 JSON，不要输出额外解释。\n\n"
            f"输入：{json.dumps(enriched_payload, ensure_ascii=False)}"
        )

        if self.debug:
            print("[质量闸门] 发送给模型的输入条数:", len(items))

        raw = client.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            timeout=self.timeout,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )

        extracted = _safe_json_extract(raw)
        if not extracted:
            raise ValueError("模型输出未找到可解析的 JSON")

        data = json.loads(extracted)
        if not isinstance(data, list):
            raise ValueError("模型输出 JSON 不是数组")

        decisions: Dict[str, Dict[str, Any]] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id", "")).strip()
            if not rid:
                continue
            brief = str(row.get("brief", "")).strip()
            if brief:
                # 轻量截断，避免单条过长影响推送
                brief = brief.replace("\n", " ").replace("\r", " ").strip()
                if len(brief) > 80:
                    brief = brief[:80].rstrip()
            decisions[rid] = {
                "keep": bool(row.get("keep", False)),
                "score": int(row.get("score", 0) or 0),
                "reason": str(row.get("reason", "")).strip(),
                "brief": brief,
            }

        return decisions

    def _apply_kept_ids(
        self,
        report_data: Dict[str, Any],
        rss_items: Optional[List[Dict[str, Any]]],
        rss_new_items: Optional[List[Dict[str, Any]]],
        keep_ids: set,
        pointers: Dict[str, Dict[str, Any]],
        decisions: Dict[str, Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]:
        # 深拷贝不是必须（report_data 在后续只用于推送），但避免副作用更安全
        import copy

        report_data = copy.deepcopy(report_data or {})
        rss_items = copy.deepcopy(rss_items) if rss_items else None
        rss_new_items = copy.deepcopy(rss_new_items) if rss_new_items else None

        # 热榜 stats
        new_stats = []
        for gi, g in enumerate(report_data.get("stats", []) or []):
            titles = g.get("titles", []) or []
            kept_titles = []
            for ti, t in enumerate(titles):
                tid = f"hot_stats:{gi}:{ti}"
                if tid in keep_ids:
                    d = decisions.get(tid) or {}
                    if d.get("brief"):
                        t["brief"] = d["brief"]
                    kept_titles.append(t)
            if kept_titles:
                g["titles"] = kept_titles
                g["count"] = len(kept_titles)
                new_stats.append(g)
        report_data["stats"] = new_stats

        # 热榜 new_titles
        new_sources = []
        for si, s in enumerate(report_data.get("new_titles", []) or []):
            titles = s.get("titles", []) or []
            kept_titles = []
            for ti, t in enumerate(titles):
                tid = f"hot_new:{si}:{ti}"
                if tid in keep_ids:
                    d = decisions.get(tid) or {}
                    if d.get("brief"):
                        t["brief"] = d["brief"]
                    kept_titles.append(t)
            if kept_titles:
                s["titles"] = kept_titles
                new_sources.append(s)
        report_data["new_titles"] = new_sources
        report_data["total_new_count"] = sum(len(s.get("titles", []) or []) for s in new_sources)

        # RSS stats
        if rss_items is not None:
            new_rss_stats = []
            for gi, g in enumerate(rss_items or []):
                titles = g.get("titles", []) or []
                kept_titles = []
                for ti, t in enumerate(titles):
                    tid = f"rss_stats:{gi}:{ti}"
                    if tid in keep_ids:
                        d = decisions.get(tid) or {}
                        if d.get("brief"):
                            t["brief"] = d["brief"]
                        kept_titles.append(t)
                if kept_titles:
                    g["titles"] = kept_titles
                    g["count"] = len(kept_titles)
                    new_rss_stats.append(g)
            rss_items = new_rss_stats

        # RSS new
        if rss_new_items is not None:
            new_rss_new = []
            for gi, g in enumerate(rss_new_items or []):
                titles = g.get("titles", []) or []
                kept_titles = []
                for ti, t in enumerate(titles):
                    tid = f"rss_new:{gi}:{ti}"
                    if tid in keep_ids:
                        d = decisions.get(tid) or {}
                        if d.get("brief"):
                            t["brief"] = d["brief"]
                        kept_titles.append(t)
                if kept_titles:
                    g["titles"] = kept_titles
                    g["count"] = len(kept_titles)
                    new_rss_new.append(g)
            rss_new_items = new_rss_new

        return report_data, rss_items, rss_new_items

