#!/usr/bin/env python3
# coding=utf-8
"""
正文抓取与轻量抽取（用于质量闸门/精华总结）

设计目标：
- 快速、稳定：失败自动返回 None，不阻塞主流程
- 成本可控：只抽取前 N 字符作为“内容片段”
- 依赖最小：使用 requests（已在 requirements.txt）
"""

from __future__ import annotations

import re
from typing import Optional

import requests


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _strip_html(html: str) -> str:
    """极简 HTML -> 文本（不追求完美，只求稳）。"""
    if not html:
        return ""

    # 删除 script/style/noscript
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    # 删除 HTML 标签
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    # 解码最常见的实体（避免引入额外依赖）
    html = (
        html.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # 合并空白
    html = re.sub(r"[ \t\r\n\x0b\x0c]+", " ", html).strip()
    return html


def fetch_url_text(
    url: str,
    timeout: int = 10,
    max_chars: int = 4000,
) -> Optional[str]:
    """
    抓取 URL 并抽取一段文本。

    Returns:
        str: 抽取到的文本片段（长度 <= max_chars）
        None: 抓取/解析失败或文本过短
    """
    if not url or not isinstance(url, str):
        return None

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _UA},
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return None
        text = resp.text or ""
    except Exception:
        return None

    cleaned = _strip_html(text)
    if not cleaned:
        return None

    # 太短的内容没有信息量
    if len(cleaned) < 200:
        return None

    if max_chars and max_chars > 0:
        cleaned = cleaned[:max_chars]

    return cleaned

