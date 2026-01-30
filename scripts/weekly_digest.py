# coding=utf-8
"""
每周深度简报（默认不依赖 LLM，可选启用 LLM 增强）

用途：
- 基于本地 output 数据（可选从远程存储拉取）生成「每周摘要」Markdown
- 将摘要保存到 output/weekly/<YYYY-MM-DD>/weekly.md
- 如配置了通用 Webhook，则将 Markdown 推送到该 Webhook（便于二次分发）

说明：
- 周报生成逻辑复用 MCP AnalyticsTools.generate_summary_report(report_type="weekly")
- 推送仅使用 generic_webhook（如果你希望周报也走飞书/钉钉/Telegram 等渠道，
  后续可以再扩展一个“自定义消息推送”能力）

可选：LLM 增强（用于“每周用 gemini-3-pro-preview”这种调度需求）
- 设置环境变量 WEEKLY_LLM_ENABLED=true
- 会在周报顶部追加一段「AI 研判摘要」（模型/网关由 AI_MODEL / AI_API_BASE / AI_API_KEY 控制）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

# 允许以脚本方式执行（python scripts/weekly_digest.py）时正确导入项目包
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from trendradar.core import load_config
from trendradar.core.config import parse_multi_account_config
from trendradar.analysis.external_leaderboards import render_weekly_external_leaderboards_markdown
from trendradar.storage.manager import get_storage_manager
from trendradar.utils.time import get_configured_time
from mcp_server.tools.analytics import AnalyticsTools


def _render_generic_payload(payload_template: str, title: str, content: str) -> dict:
    """
    渲染通用 Webhook payload。

    模板支持：
    - {title}
    - {content}
    """
    if not payload_template:
        return {"title": title, "content": content}

    # 注意：content/title 可能包含引号、换行等，需要先 JSON 转义后再做模板替换
    json_content = json.dumps(content, ensure_ascii=False)[1:-1]  # 去掉首尾引号
    json_title = json.dumps(title, ensure_ascii=False)[1:-1]

    payload_str = payload_template.replace("{content}", json_content).replace("{title}", json_title)
    try:
        return json.loads(payload_str)
    except json.JSONDecodeError:
        # 模板不合法时回退到默认格式，避免整个周报失败
        return {"title": title, "content": content}

def _is_truthy_env(key: str) -> bool:
    value = (os.environ.get(key, "") or "").strip().lower()
    return value in ("1", "true", "yes", "y", "on")

def _is_truthy_env_default(key: str, default: bool) -> bool:
    raw = (os.environ.get(key, "") or "").strip()
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _generate_llm_insights(markdown_report: str, config: dict) -> str:
    """
    可选：用 LLM 对周报做“高层研判摘要”。
    返回值为空字符串表示跳过。
    """
    if not _is_truthy_env("WEEKLY_LLM_ENABLED"):
        return ""

    # 延迟导入：本地环境未安装 litellm 时，不影响周报主链路
    try:
        from trendradar.ai.client import AIClient  # pylint: disable=import-error
    except Exception as e:
        print(f"[Weekly] LLM 依赖不可用，已跳过: {type(e).__name__}: {e}")
        return ""

    # 避免超长输入导致失败：做一个简单截断
    max_chars = 30000
    content_for_llm = markdown_report
    if len(content_for_llm) > max_chars:
        content_for_llm = content_for_llm[:max_chars] + "\n\n...(内容过长，已截断)...\n"

    ai_cfg = config.get("AI", {}) if isinstance(config, dict) else {}
    client = AIClient(ai_cfg)

    system_prompt = (
        "你是资深 AI 产品经理与行业分析师。"
        "你将收到一份 TrendRadar 的「每周热点摘要」（基于多平台标题聚合，不包含完整正文）。"
        "请输出一段可直接发给团队的「AI 研判摘要」，强调：关键趋势、重要信号、竞品动态、风险与机会、下周行动建议。"
        "要求：中文、结构化、短而密、避免空话。"
    )
    user_prompt = (
        "请基于下列周报内容生成「AI 研判摘要」。\n\n"
        "输出格式：\n"
        "## 🤖 AI 研判摘要\n"
        "- 核心结论：...\n"
        "- 关键趋势：...\n"
        "- 异动/弱信号：...\n"
        "- 竞品/市场：...\n"
        "- 风险与机会：...\n"
        "- 下周行动建议：...\n\n"
        "周报内容如下：\n\n"
        f"{content_for_llm}"
    )

    try:
        llm_text = client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        ).strip()
        return llm_text or ""
    except Exception as e:
        print(f"[Weekly] LLM 增强失败，已跳过: {type(e).__name__}: {e}")
        return ""


def _generate_weekly_leaderboard(project_root: str, _now: datetime, _config: dict) -> str:
    """
    可选：生成“模型/工具热度排行榜”区块（默认：外部权威口径）。

    开关与口径：
    - WEEKLY_LEADERBOARD_ENABLED: 总开关（默认 true）
    - WEEKLY_LEADERBOARD_SOURCE: external/internal（默认 external）
    - WEEKLY_EXTERNAL_LEADERBOARD_ENABLED: 外部榜单开关（默认 true）
    - WEEKLY_LEADERBOARD_TOP_N: Top N（默认 10）
    """
    if not _is_truthy_env_default("WEEKLY_LEADERBOARD_ENABLED", default=True):
        return ""

    source = (os.environ.get("WEEKLY_LEADERBOARD_SOURCE", "") or "external").strip().lower()
    if source not in ("external", "internal"):
        source = "external"

    if source == "external" and not _is_truthy_env_default("WEEKLY_EXTERNAL_LEADERBOARD_ENABLED", default=True):
        return ""

    top_n = _get_env_int("WEEKLY_LEADERBOARD_TOP_N", default=10)
    if top_n <= 0:
        return ""

    try:
        if source == "internal":
            # 兼容保留：需要时可切回内部“热榜标题命中统计”方案（不推荐，默认已切到 external）
            from trendradar.analysis.leaderboard import render_weekly_leaderboard_markdown  # pylint: disable=import-error

            # 内部榜单仍沿用原有权重配置口径
            weight_cfg = _config.get("WEIGHT_CONFIG") if isinstance(_config, dict) else None
            rank_threshold = int(_config.get("RANK_THRESHOLD", 5)) if isinstance(_config, dict) else 5
            return render_weekly_leaderboard_markdown(
                project_root=project_root,
                end_time=_now,
                top_n=top_n,
                rank_threshold=rank_threshold,
                weight_config=weight_cfg,
            )

        # external
        return render_weekly_external_leaderboards_markdown(
            project_root=project_root,
            top_n=top_n,
        )
    except Exception as e:
        print(f"[Weekly] 排行榜生成失败，已跳过: {type(e).__name__}: {e}")
        return ""


def main() -> None:
    config = load_config()
    timezone = config.get("TIMEZONE", "Asia/Shanghai")
    now = get_configured_time(timezone)

    # 启动时可选拉取：从远程拉取最近 N 天数据到本地 output（供周报/排行榜读取）
    storage_config = config.get("STORAGE", {}) if isinstance(config, dict) else {}
    remote_config = (storage_config.get("REMOTE", {}) or {}) if isinstance(storage_config, dict) else {}
    local_config = (storage_config.get("LOCAL", {}) or {}) if isinstance(storage_config, dict) else {}
    pull_config = (storage_config.get("PULL", {}) or {}) if isinstance(storage_config, dict) else {}

    storage = get_storage_manager(
        backend_type=storage_config.get("BACKEND", "auto") if isinstance(storage_config, dict) else "auto",
        data_dir=local_config.get("DATA_DIR", "output") if isinstance(local_config, dict) else "output",
        enable_txt=(storage_config.get("FORMATS", {}) or {}).get("TXT", True) if isinstance(storage_config, dict) else True,
        enable_html=(storage_config.get("FORMATS", {}) or {}).get("HTML", True) if isinstance(storage_config, dict) else True,
        remote_config={
            "bucket_name": remote_config.get("BUCKET_NAME", ""),
            "access_key_id": remote_config.get("ACCESS_KEY_ID", ""),
            "secret_access_key": remote_config.get("SECRET_ACCESS_KEY", ""),
            "endpoint_url": remote_config.get("ENDPOINT_URL", ""),
            "region": remote_config.get("REGION", ""),
        },
        local_retention_days=local_config.get("RETENTION_DAYS", 0) if isinstance(local_config, dict) else 0,
        remote_retention_days=remote_config.get("RETENTION_DAYS", 0) if isinstance(remote_config, dict) else 0,
        pull_enabled=pull_config.get("ENABLED", False) if isinstance(pull_config, dict) else False,
        pull_days=pull_config.get("DAYS", 7) if isinstance(pull_config, dict) else 7,
        timezone=timezone,
        force_new=True,
    )
    pulled = storage.pull_from_remote()
    if pulled:
        print(f"[Weekly] 已从远程拉取 {pulled} 个文件到本地 output")

    # 生成周报（读取 output 中最近 7 天的数据）
    project_root = str(Path(__file__).resolve().parents[1])
    tools = AnalyticsTools(project_root=project_root)
    result = tools.generate_summary_report(report_type="weekly")
    if not result.get("success"):
        raise SystemExit(f"weekly_digest 生成失败: {result.get('error')}")

    markdown_report = result.get("markdown_report", "").strip()
    if not markdown_report:
        raise SystemExit("weekly_digest 生成失败: markdown_report 为空")

    base_report = markdown_report

    # 可选：LLM 研判摘要（放在最前）
    llm_section = _generate_llm_insights(base_report, config)

    # 可选：排行榜（放在 AI 研判摘要之后、正文之前）
    leaderboard_section = _generate_weekly_leaderboard(project_root, now, config)

    parts = []
    if llm_section:
        parts.append(llm_section.strip())
    if leaderboard_section:
        parts.append(leaderboard_section.strip())
    parts.append(base_report.strip())

    markdown_report = "\n\n---\n\n".join([p for p in parts if p])

    date_str = now.strftime("%Y-%m-%d")
    title = f"TrendRadar 每周深度简报 - {date_str}"

    # 保存到本地 output（便于回溯/复盘/分享）
    out_dir = Path("output") / "weekly" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "weekly.md"
    md_path.write_text(markdown_report + "\n", encoding="utf-8")
    print(f"[Weekly] 报告已生成: {md_path}")

    # 可选推送：通用 Webhook（用于二次分发）
    webhook_urls = parse_multi_account_config(config.get("GENERIC_WEBHOOK_URL", ""))
    payload_template = config.get("GENERIC_WEBHOOK_TEMPLATE", "")

    if not webhook_urls:
        print("[Weekly] 未配置 GENERIC_WEBHOOK_URL，跳过推送")
        return

    payload = _render_generic_payload(payload_template, title=title, content=markdown_report)

    ok_count = 0
    for i, url in enumerate(webhook_urls, 1):
        if not url:
            continue
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if 200 <= resp.status_code < 300:
                ok_count += 1
                print(f"[Weekly] 通用Webhook 账号{i} 发送成功")
            else:
                print(f"[Weekly] 通用Webhook 账号{i} 发送失败: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[Weekly] 通用Webhook 账号{i} 发送异常: {e}")

    if ok_count == 0:
        raise SystemExit("[Weekly] 所有通用Webhook发送均失败")


if __name__ == "__main__":
    main()

