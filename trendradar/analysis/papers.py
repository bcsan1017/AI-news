# coding=utf-8
"""
论文专区：新增论文监控与单篇解读报告生成（用于 GitHub Pages 链接）

设计目标：
- 在增量运行时，从指定 arXiv RSS feeds 中找出新增论文
- 通过 LLM（gemini-3-pro-preview）筛选“高价值”论文并生成可读性高的解读报告
- 报告落盘到 site/ 目录，供 GitHub Pages 发布为固定链接

约束与防幻觉：
- 仅基于 RSS 条目（标题/摘要/链接）分析，禁止臆造实验细节
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


_ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/([^?#]+)", re.IGNORECASE)
_ARXIV_PDF_RE = re.compile(r"arxiv\.org/pdf/([^?#]+?)(?:\.pdf)?$", re.IGNORECASE)
_AR5IV_HTML_RE = re.compile(r"ar5iv\.labs\.arxiv\.org/html/([^?#]+)$", re.IGNORECASE)


def _safe_json_extract(text: str) -> Optional[str]:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        lines = s.splitlines()
        if lines and lines[0].strip().lower() in ("json", "javascript"):
            s = "\n".join(lines[1:]).strip()
    l = s.find("[")
    r = s.rfind("]")
    if l != -1 and r != -1 and r > l:
        return s[l : r + 1].strip()
    l = s.find("{")
    r = s.rfind("}")
    if l != -1 and r != -1 and r > l:
        return s[l : r + 1].strip()
    return None


def _load_prompt_template(prompt_path: Path) -> Tuple[str, str]:
    """
    兼容 TrendRadar 现有 prompt 格式：
    - 含 [system] 与 [user] 分段
    - 或只有纯文本（视为 user）
    """
    content = prompt_path.read_text(encoding="utf-8")
    if "[system]" in content and "[user]" in content:
        parts = content.split("[user]", 1)
        system_part = parts[0]
        user_part = parts[1] if len(parts) > 1 else ""
        system_prompt = system_part.split("[system]", 1)[1].strip() if "[system]" in system_part else ""
        user_prompt = user_part.strip()
        return system_prompt, user_prompt
    return "", content.strip()


def _guess_pages_base_url(config_base: str = "") -> str:
    """
    推导 GitHub Pages base url：
    - 优先使用配置/环境变量提供的固定 base url
    - 否则根据 GITHUB_REPOSITORY 推导为 https://{owner}.github.io/{repo}/
    """
    if config_base:
        base = config_base.strip()
        if base and not base.endswith("/"):
            base += "/"
        return base

    repo = (os.environ.get("GITHUB_REPOSITORY", "") or "").strip()
    if not repo or "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/"


def _extract_arxiv_id(url: str) -> str:
    if not url:
        return ""
    m = _ARXIV_ABS_RE.search(url)
    if m:
        return m.group(1).strip()
    # 兜底：取最后路径段
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path or ""
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else ""
    except Exception:
        return ""


def _normalize_arxiv_id(arxiv_id: str) -> str:
    s = (arxiv_id or "").strip()
    if not s:
        return ""
    s = s.replace(".pdf", "")
    s = s.strip("/")
    return s


def _arxiv_abs_url(arxiv_id: str) -> str:
    aid = _normalize_arxiv_id(arxiv_id)
    return f"https://arxiv.org/abs/{aid}" if aid else ""


def _arxiv_pdf_url(arxiv_id: str) -> str:
    aid = _normalize_arxiv_id(arxiv_id)
    return f"https://arxiv.org/pdf/{aid}.pdf" if aid else ""


def _ar5iv_url(arxiv_id: str) -> str:
    aid = _normalize_arxiv_id(arxiv_id)
    return f"https://ar5iv.labs.arxiv.org/html/{aid}" if aid else ""


def _clip_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if not max_chars or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    # 保留头尾，尽量覆盖信息密度（防止只截到参考文献）
    head = text[: int(max_chars * 0.7)]
    tail = text[-int(max_chars * 0.3) :]
    return head + "\n\n...[TRUNCATED]...\n\n" + tail


def _fetch_text_from_ar5iv(arxiv_id: str, timeout: int) -> Optional[str]:
    url = _ar5iv_url(arxiv_id)
    if not url:
        return None
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "TrendRadar/2.0"})
    if resp.status_code != 200:
        return None
    html_text = resp.text or ""
    if not html_text.strip():
        return None
    # 粗略去标签：保留段落文本
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html_text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    # 仅折叠“空白字符”，不要误删字母（避免错误的转义导致 t/x/b/c/r 被当作可替换字符）
    text = re.sub(r"[ \t\x0b\x0c\r]+", " ", text)
    text = re.sub(r"\\n{3,}", "\\n\\n", text)
    text = text.strip()
    # 极端短则视为失败
    return text if len(text) >= 2000 else None


def _fetch_text_from_arxiv_abs(arxiv_id: str, timeout: int) -> Optional[str]:
    url = _arxiv_abs_url(arxiv_id)
    if not url:
        return None
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "TrendRadar/2.0"})
    if resp.status_code != 200:
        return None
    html_text = resp.text or ""
    if not html_text.strip():
        return None
    # 提取 abstract（arXiv 页面有 <blockquote class="abstract">）
    m = re.search(r'(?is)<blockquote[^>]*class="abstract[^"]*"[^>]*>(.*?)</blockquote>', html_text)
    if not m:
        return None
    block = m.group(1)
    block = re.sub(r"(?is)<[^>]+>", " ", block)
    block = re.sub(r"[ \t\x0b\x0c\r]+", " ", block).strip()
    # 修复 arXiv 页面偶发的“逐字符分隔”问题：把单字母 token 重新拼回单词
    # 示例：A b s t r a c t : T h i s ... -> Abstract: This ...
    block = re.sub(r"(?<=\\b[A-Za-z])\\s+(?=[A-Za-z]\\b)", "", block)
    return block if len(block) >= 200 else None


def _fetch_text_from_arxiv_pdf(arxiv_id: str, timeout: int) -> Optional[str]:
    url = _arxiv_pdf_url(arxiv_id)
    if not url:
        return None
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "TrendRadar/2.0"})
    if resp.status_code != 200 or not resp.content:
        return None
    try:
        from pypdf import PdfReader
    except Exception:
        return None

    try:
        import io

        reader = PdfReader(io.BytesIO(resp.content))
        texts = []
        # 只抽取前若干页以控制成本与噪声（全文太长）
        max_pages = min(len(reader.pages), 12)
        for i in range(max_pages):
            try:
                page_text = reader.pages[i].extract_text() or ""
                if page_text.strip():
                    texts.append(page_text)
            except Exception:
                continue
        joined = "\n\n".join(texts).strip()
        return joined if len(joined) >= 1500 else None
    except Exception:
        return None


def fetch_paper_content(
    candidate: "PaperCandidate",
    paper_zone_cfg: Dict[str, Any],
) -> Tuple[str, str]:
    """
    获取论文“尽可能接近原文”的文本内容。
    返回：(content_source, paper_content)
    """
    priority = paper_zone_cfg.get("CONTENT_SOURCE_PRIORITY") or ["ar5iv", "pdf", "abs", "rss"]
    timeout = int(paper_zone_cfg.get("CONTENT_FETCH_TIMEOUT") or 20)
    max_chars = int(paper_zone_cfg.get("MAX_CONTENT_CHARS") or 45000)

    arxiv_id = candidate.arxiv_id or _extract_arxiv_id(candidate.url)
    arxiv_id = _normalize_arxiv_id(arxiv_id)

    # fallback：rss summary
    rss_fallback = candidate.summary or ""

    for src in priority:
        s = (src or "").strip().lower()
        try:
            if s == "ar5iv" and arxiv_id:
                text = _fetch_text_from_ar5iv(arxiv_id, timeout)
                if text:
                    return "ar5iv", _clip_text(text, max_chars)
            if s == "pdf" and arxiv_id:
                text = _fetch_text_from_arxiv_pdf(arxiv_id, timeout)
                if text:
                    return "pdf", _clip_text(text, max_chars)
            if s == "abs" and arxiv_id:
                text = _fetch_text_from_arxiv_abs(arxiv_id, timeout)
                if text:
                    return "abs", _clip_text(text, max_chars)
            if s == "rss":
                if rss_fallback.strip():
                    return "rss", _clip_text(rss_fallback, max_chars)
        except Exception:
            continue

    # 兜底
    return ("rss" if rss_fallback.strip() else "unknown"), _clip_text(rss_fallback, max_chars)


def _slugify(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = s.replace("/", "_")
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s.strip("_").lower()


def _md_inline_format(text: str) -> str:
    # 简单处理：链接与粗体
    t = _html.escape(text, quote=False)
    # **bold**
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    # [text](url)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<a href=\"\2\" target=\"_blank\" rel=\"noopener noreferrer\">\1</a>", t)
    return t


def _markdown_to_html(md: str) -> str:
    """
    轻量 markdown 渲染器（覆盖本项目输出结构：标题/列表/段落/代码块/链接/粗体）。
    目标：不引入额外依赖，也能在 Pages 上可读。
    """
    if md is None:
        return ""
    lines = md.splitlines()
    out: List[str] = []
    in_code = False
    in_ul = False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip("\n")
        if line.strip().startswith("```"):
            close_ul()
            if not in_code:
                in_code = True
                out.append("<pre><code>")
            else:
                in_code = False
                out.append("</code></pre>")
            continue

        if in_code:
            out.append(_html.escape(line))
            continue

        if not line.strip():
            close_ul()
            out.append("")
            continue

        # headings
        if line.startswith("### "):
            close_ul()
            out.append(f"<h3>{_md_inline_format(line[4:].strip())}</h3>")
            continue
        if line.startswith("## "):
            close_ul()
            out.append(f"<h2>{_md_inline_format(line[3:].strip())}</h2>")
            continue
        if line.startswith("# "):
            close_ul()
            out.append(f"<h1>{_md_inline_format(line[2:].strip())}</h1>")
            continue

        # unordered list
        if line.lstrip().startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            item = line.lstrip()[2:].strip()
            out.append(f"<li>{_md_inline_format(item)}</li>")
            continue

        # paragraph
        close_ul()
        out.append(f"<p>{_md_inline_format(line.strip())}</p>")

    close_ul()
    # 清理连续空行
    html_text = "\n".join(out)
    html_text = re.sub(r"\n{3,}", "\n\n", html_text)
    return html_text


@dataclass
class PaperCandidate:
    id: str
    feed_id: str
    feed_name: str
    title: str
    url: str
    published_at: str
    summary: str

    arxiv_id: str = ""
    slug: str = ""


@dataclass
class PaperDecision:
    id: str
    score: int
    keep: bool
    reason: str


def collect_paper_candidates(
    raw_rss_items: Optional[List[Dict[str, Any]]],
    feed_ids: List[str],
) -> List[PaperCandidate]:
    if not raw_rss_items or not feed_ids:
        return []
    feed_id_set = {x.strip() for x in feed_ids if x and str(x).strip()}
    candidates: List[PaperCandidate] = []
    for item in raw_rss_items:
        fid = (item.get("feed_id") or "").strip()
        if fid not in feed_id_set:
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title or not url:
            continue
        published_at = (item.get("published_at") or "").strip()
        summary = (item.get("summary") or "").strip()
        feed_name = (item.get("feed_name") or fid).strip()
        pid = _extract_arxiv_id(url) or url
        slug = _slugify(pid) or _slugify(title[:80])
        candidates.append(
            PaperCandidate(
                id=pid,
                feed_id=fid,
                feed_name=feed_name,
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                arxiv_id=_extract_arxiv_id(url),
                slug=slug,
            )
        )
    return candidates


def decide_high_value_papers(
    candidates: List[PaperCandidate],
    ai_config: Dict[str, Any],
    model: str,
    reasoning_effort: str,
    min_score: int,
    max_reports_per_run: int,
    timeout: int = 90,
) -> List[PaperDecision]:
    """
    用 LLM 对候选论文做“高价值”判定与打分，并返回 top K。
    """
    if not candidates:
        return []

    try:
        from trendradar.ai.client import AIClient
    except Exception as e:
        raise RuntimeError(f"论文解读依赖不可用: {type(e).__name__}: {e}") from e

    cfg = dict(ai_config or {})
    cfg["MODEL"] = model
    # 这里不强行覆盖 MAX_TOKENS：由 AIClient 默认配置控制；只在 chat 调用时限制
    client = AIClient(cfg)

    payload = []
    for c in candidates:
        payload.append(
            {
                "id": c.id,
                "feed_id": c.feed_id,
                "title": c.title,
                "url": c.url,
                "published_at": c.published_at,
                "summary": (c.summary[:1200] if c.summary else ""),
            }
        )

    system = (
        "你是一名 AI 研究与产品评审官。你要从“新论文标题+摘要”中评估其对 AI 产品经理的价值。\n"
        "高价值的判定标准（优先级从高到低）：\n"
        "1) 直接推动模型/Agent/多模态能力边界或评测方法\n"
        "2) 明确可落地的工程方法（效率、可靠性、安全、可控性）\n"
        "3) 与端侧/可穿戴/XR/HCI 交互强相关（隐私、低功耗、实时）\n\n"
        "必须避免：仅凭标题猜测细节；如果摘要不支持结论，要降低评分并说明。\n"
        "输出必须是 JSON 数组（不要输出解释）。"
    )
    user = (
        "请对每条论文输出一个对象：\n"
        '- id: 与输入一致\n'
        '- keep: true/false（是否值得生成“单篇解读报告”）\n'
        '- score: 0-100（价值评分）\n'
        '- reason: 简短原因（<=20字）\n\n'
        f"输入：{json.dumps(payload, ensure_ascii=False)}"
    )

    raw = client.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        timeout=timeout,
        max_tokens=1400,
        reasoning_effort=reasoning_effort,
    )

    extracted = _safe_json_extract(raw)
    if not extracted:
        raise ValueError("高价值判定输出未找到可解析 JSON")
    data = json.loads(extracted)
    if not isinstance(data, list):
        raise ValueError("高价值判定输出不是 JSON 数组")

    decisions: List[PaperDecision] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id", "")).strip()
        if not rid:
            continue
        score = int(row.get("score", 0) or 0)
        keep = bool(row.get("keep", False))
        reason = str(row.get("reason", "")).strip()
        decisions.append(PaperDecision(id=rid, score=score, keep=keep, reason=reason))

    # 过滤与排序
    picked = [d for d in decisions if d.keep and d.score >= int(min_score or 0)]
    picked.sort(key=lambda x: (-x.score, x.id))

    if max_reports_per_run and max_reports_per_run > 0:
        picked = picked[:max_reports_per_run]
    return picked


def generate_single_paper_report(
    candidate: PaperCandidate,
    ai_config: Dict[str, Any],
    paper_zone_cfg: Dict[str, Any],
    now: datetime,
    project_root: str,
) -> Tuple[str, str]:
    """
    生成单篇报告：返回 (markdown, html)
    """
    try:
        from trendradar.ai.client import AIClient
    except Exception as e:
        raise RuntimeError(f"论文解读依赖不可用: {type(e).__name__}: {e}") from e

    model = (paper_zone_cfg.get("MODEL") or "gemini-3-pro-preview").strip()
    reasoning_effort = (paper_zone_cfg.get("REASONING_EFFORT") or "high").strip()

    cfg = dict(ai_config or {})
    cfg["MODEL"] = model
    client = AIClient(cfg)

    config_dir = Path(project_root) / "config"
    prompt_file = (paper_zone_cfg.get("PROMPT_FILE") or "paper_analysis_prompt.txt").strip()
    prompt_path = config_dir / prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(f"论文解读提示词不存在: {prompt_path}")

    system_prompt, user_template = _load_prompt_template(prompt_path)

    user_prompt = user_template
    content_source, paper_content = fetch_paper_content(candidate, paper_zone_cfg)
    user_prompt = user_prompt.replace("{paper_title}", candidate.title)
    user_prompt = user_prompt.replace("{paper_url}", candidate.url)
    user_prompt = user_prompt.replace("{content_source}", content_source)
    user_prompt = user_prompt.replace("{paper_content}", paper_content)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    md = client.chat(
        messages=messages,
        temperature=0.2,
        timeout=paper_zone_cfg.get("TIMEOUT", 180) or 180,
        max_tokens=paper_zone_cfg.get("MAX_TOKENS", 8000) or 8000,
        reasoning_effort=reasoning_effort,
    ).strip()

    body_html = _markdown_to_html(md)
    title_esc = _html.escape(candidate.title)
    paper_url_esc = _html.escape(candidate.url, quote=True)

    full_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title_esc} - TrendRadar Paper Brief</title>
  <style>
    :root {{
      color-scheme: light dark;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", sans-serif;
      line-height: 1.6;
      margin: 0;
      padding: 0;
    }}
    header {{
      padding: 24px 16px;
      border-bottom: 1px solid rgba(127,127,127,0.25);
    }}
    main {{
      max-width: 920px;
      margin: 0 auto;
      padding: 20px 16px 56px;
    }}
    a {{ color: inherit; }}
    code, pre {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace;
    }}
    pre {{
      padding: 12px;
      border-radius: 8px;
      overflow: auto;
      background: rgba(127,127,127,0.12);
    }}
    h1, h2, h3 {{ line-height: 1.25; }}
    .meta {{
      color: rgba(127,127,127,0.9);
      font-size: 14px;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid rgba(127,127,127,0.35);
      font-size: 12px;
      margin-right: 8px;
    }}
  </style>
</head>
<body>
  <header>
    <div class="meta">
      <span class="badge">TrendRadar</span>
      <span class="badge">Paper Brief</span>
      <span class="badge">{_html.escape(candidate.feed_name)}</span>
      <span>生成时间：{_html.escape(now.strftime("%Y-%m-%d %H:%M:%S"))}</span>
    </div>
    <h1>{title_esc}</h1>
    <div class="meta">
      <a href="{paper_url_esc}" target="_blank" rel="noopener noreferrer">原文链接</a>
      {" · 发布时间：" + _html.escape(candidate.published_at) if candidate.published_at else ""}
    </div>
  </header>
  <main>
    {body_html}
  </main>
</body>
</html>
"""
    return md, full_html


def write_paper_pages(
    candidates: List[PaperCandidate],
    decisions: List[PaperDecision],
    ai_config: Dict[str, Any],
    paper_zone_cfg: Dict[str, Any],
    now: datetime,
    project_root: str,
) -> List[Dict[str, Any]]:
    """
    生成并写入页面，返回用于推送的 paper_reports 列表。
    """
    output_dir = (paper_zone_cfg.get("OUTPUT_DIR") or "site").strip()
    site_dir = Path(project_root) / output_dir
    papers_dir = site_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    base_url = _guess_pages_base_url(paper_zone_cfg.get("PAGES_BASE_URL", ""))

    # 建立 id -> candidate
    cand_map = {c.id: c for c in candidates}

    reports: List[Dict[str, Any]] = []
    for d in decisions:
        c = cand_map.get(d.id)
        if not c:
            continue
        slug = c.slug or _slugify(c.id)
        if not slug:
            continue

        out_path = papers_dir / slug / "index.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 若已存在则不重复生成（避免重复消耗）
        if not out_path.exists():
            md, html_text = generate_single_paper_report(
                candidate=c,
                ai_config=ai_config,
                paper_zone_cfg=paper_zone_cfg,
                now=now,
                project_root=project_root,
            )
            out_path.write_text(html_text, encoding="utf-8")
            # 旁路保存 markdown（方便 diff/复用）
            (out_path.parent / "report.md").write_text(md, encoding="utf-8")

        report_url = f"{base_url}papers/{slug}/" if base_url else f"papers/{slug}/"
        reports.append(
            {
                "title": c.title,
                "paper_url": c.url,
                "report_url": report_url,
                "feed_name": c.feed_name,
                "feed_id": c.feed_id,
                "published_at": c.published_at,
                "score": d.score,
                "reason": d.reason,
                "slug": slug,
                "local_path": str(out_path),
            }
        )

    # 生成索引页（简洁可读）
    _write_papers_index(papers_dir, reports, now, base_url)
    _write_root_index(site_dir, now, base_url)

    return reports


def _write_papers_index(papers_dir: Path, reports: List[Dict[str, Any]], now: datetime, base_url: str) -> None:
    # 按 score / 时间排序（时间可能缺失，先用 score）
    sorted_reports = sorted(reports, key=lambda x: (-int(x.get("score", 0) or 0), x.get("published_at", "")),)
    items = []
    for r in sorted_reports[:200]:
        title = _html.escape(r.get("title", ""))
        url = _html.escape(r.get("report_url", ""), quote=True)
        score = int(r.get("score", 0) or 0)
        feed = _html.escape(r.get("feed_name", ""))
        items.append(f"<li><a href=\"{url}\">{title}</a> <span class=\"meta\">({feed} · score {score})</span></li>")

    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>TrendRadar 论文专区</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", sans-serif; line-height: 1.6; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 24px 16px 56px; }}
    .meta {{ color: rgba(127,127,127,0.9); font-size: 13px; }}
    a {{ color: inherit; }}
  </style>
</head>
<body>
  <main>
    <h1>📚 TrendRadar 论文专区</h1>
    <div class="meta">更新时间：{_html.escape(now.strftime("%Y-%m-%d %H:%M:%S"))}</div>
    <p class="meta">说明：此页面列出近期由 TrendRadar 自动生成的论文解读报告（单篇）。</p>
    <ul>
      {''.join(items) if items else '<li class="meta">暂无报告</li>'}
    </ul>
    <hr/>
    <div class="meta">Base URL：{_html.escape(base_url) if base_url else '未配置（将以相对链接展示）'}</div>
  </main>
</body>
</html>
"""
    (papers_dir / "index.html").write_text(html_text, encoding="utf-8")


def _write_root_index(site_dir: Path, now: datetime, base_url: str) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    papers_url = f"{base_url}papers/" if base_url else "papers/"
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>TrendRadar Reports</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", sans-serif; line-height: 1.6; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 24px 16px 56px; }}
    .meta {{ color: rgba(127,127,127,0.9); font-size: 13px; }}
    a {{ color: inherit; }}
  </style>
</head>
<body>
  <main>
    <h1>TrendRadar Reports</h1>
    <div class="meta">更新时间：{_html.escape(now.strftime("%Y-%m-%d %H:%M:%S"))}</div>
    <ul>
      <li><a href=\"{_html.escape(papers_url, quote=True)}\">论文专区（Papers）</a></li>
    </ul>
  </main>
</body>
</html>
"""
    (site_dir / "index.html").write_text(html_text, encoding="utf-8")

