# coding=utf-8
"""
RSS 抓取器

负责从配置的 RSS 源抓取数据并转换为标准格式
"""

import time
import random
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Callable

import requests

from .parser import RSSParser, ParsedRSSItem
from trendradar.storage.base import RSSItem, RSSData
from trendradar.utils.time import get_configured_time, is_within_days, DEFAULT_TIMEZONE


@dataclass
class RSSFeedConfig:
    """RSS 源配置"""
    id: str                     # 源 ID
    name: str                   # 显示名称
    url: str                    # RSS URL
    bearer_token: Optional[str] = None  # 可选：用于需要鉴权的源（如 x:// 用户源）
    max_items: int = 0          # 最大条目数（0=不限制）
    enabled: bool = True        # 是否启用
    max_age_days: Optional[int] = None  # 文章最大年龄（天），覆盖全局设置；None=使用全局，0=禁用过滤


class RSSFetcher:
    """RSS 抓取器"""

    def __init__(
        self,
        feeds: List[RSSFeedConfig],
        request_interval: int = 2000,
        timeout: int = 15,
        use_proxy: bool = False,
        proxy_url: str = "",
        timezone: str = DEFAULT_TIMEZONE,
        freshness_enabled: bool = True,
        default_max_age_days: int = 3,
    ):
        """
        初始化抓取器

        Args:
            feeds: RSS 源配置列表
            request_interval: 请求间隔（毫秒）
            timeout: 请求超时（秒）
            use_proxy: 是否使用代理
            proxy_url: 代理 URL
            timezone: 时区配置（如 'Asia/Shanghai'）
            freshness_enabled: 是否启用新鲜度过滤
            default_max_age_days: 默认最大文章年龄（天）
        """
        self.feeds = [f for f in feeds if f.enabled]
        self.request_interval = request_interval
        self.timeout = timeout
        self.use_proxy = use_proxy
        self.proxy_url = proxy_url
        self.timezone = timezone
        self.freshness_enabled = freshness_enabled
        self.default_max_age_days = default_max_age_days

        self.parser = RSSParser()
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """创建请求会话"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": "TrendRadar/2.0 RSS Reader (https://github.com/trendradar)",
            "Accept": "application/feed+json, application/json, application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

        if self.use_proxy and self.proxy_url:
            session.proxies = {
                "http": self.proxy_url,
                "https": self.proxy_url,
            }

        return session

    def _filter_by_freshness(
        self,
        items: List[RSSItem],
        feed: RSSFeedConfig,
    ) -> Tuple[List[RSSItem], int]:
        """
        根据新鲜度过滤文章

        Args:
            items: 待过滤的文章列表
            feed: RSS 源配置

        Returns:
            (过滤后的文章列表, 被过滤的文章数)
        """
        # 如果全局禁用，直接返回
        if not self.freshness_enabled:
            return items, 0

        # 确定此 feed 的 max_age_days
        max_days = feed.max_age_days
        if max_days is None:
            max_days = self.default_max_age_days

        # 如果设为 0，禁用此 feed 的过滤
        if max_days == 0:
            return items, 0

        # 过滤逻辑：无发布时间的文章保留
        filtered = []
        for item in items:
            if not item.published_at:
                # 无发布时间，保留
                filtered.append(item)
            elif is_within_days(item.published_at, max_days, self.timezone):
                # 在指定天数内，保留
                filtered.append(item)
            # 否则过滤掉

        filtered_count = len(items) - len(filtered)
        return filtered, filtered_count

    def fetch_feed(self, feed: RSSFeedConfig) -> Tuple[List[RSSItem], Optional[str]]:
        """
        抓取单个 RSS 源

        Args:
            feed: RSS 源配置

        Returns:
            (条目列表, 错误信息) 元组
        """
        try:
            # 支持自定义协议：x://username（通过 X 官方 API 拉取该用户的最新推文并按 RSSItem 入库）
            if feed.url.startswith("x://"):
                return self._fetch_x_user_feed(feed)

            response = self.session.get(feed.url, timeout=self.timeout)
            response.raise_for_status()

            parsed_items = self.parser.parse(response.text, feed.url)

            # 限制条目数量（0=不限制）
            if feed.max_items > 0:
                parsed_items = parsed_items[:feed.max_items]

            # 转换为 RSSItem（使用配置的时区）
            now = get_configured_time(self.timezone)
            crawl_time = now.strftime("%H:%M")
            items = []

            for parsed in parsed_items:
                item = RSSItem(
                    title=parsed.title,
                    feed_id=feed.id,
                    feed_name=feed.name,
                    url=parsed.url,
                    published_at=parsed.published_at or "",
                    summary=parsed.summary or "",
                    author=parsed.author or "",
                    crawl_time=crawl_time,
                    first_time=crawl_time,
                    last_time=crawl_time,
                    count=1,
                )
                items.append(item)

            # 注意：新鲜度过滤已移至推送阶段（_convert_rss_items_to_list）
            # 这样所有文章都会存入数据库，但旧文章不会推送
            print(f"[RSS] {feed.name}: 获取 {len(items)} 条")
            return items, None

        except requests.Timeout:
            error = f"请求超时 ({self.timeout}s)"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except requests.RequestException as e:
            error = f"请求失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except ValueError as e:
            error = f"解析失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except Exception as e:
            error = f"未知错误: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

    def _get_x_bearer_token(self, feed: RSSFeedConfig) -> Optional[str]:
        """
        获取 X API Bearer Token

        优先级：
        1) feed.bearer_token（配置文件单 feed）
        2) 环境变量 X_BEARER_TOKEN
        3) 环境变量 TWITTER_BEARER_TOKEN
        """
        token = (feed.bearer_token or "").strip() if feed.bearer_token else ""
        if token:
            return token
        token = (os.environ.get("X_BEARER_TOKEN", "") or "").strip()
        if token:
            return token
        token = (os.environ.get("TWITTER_BEARER_TOKEN", "") or "").strip()
        return token or None

    def _x_api_get(self, token: str, path: str, params: Optional[Dict] = None) -> requests.Response:
        """
        调用 X/Twitter API（兼容 api.x.com 与 api.twitter.com）
        """
        headers = {"Authorization": f"Bearer {token}"}
        params = params or {}

        # 优先尝试 api.x.com，失败后回退到 api.twitter.com（尽量提高可用性）
        base_urls = ["https://api.x.com", "https://api.twitter.com"]
        last_exc: Optional[Exception] = None
        for base in base_urls:
            try:
                url = f"{base}{path}"
                resp = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
                return resp
            except requests.RequestException as e:
                last_exc = e
                continue
        # 理论上不会到这里；兜底抛出最后一个异常
        raise last_exc or RuntimeError("X API 请求失败")

    def _fetch_x_user_feed(self, feed: RSSFeedConfig) -> Tuple[List[RSSItem], Optional[str]]:
        """
        抓取 X 用户源：x://username
        将推文转换为 RSSItem，复用现有 RSS 入库/去重/分析链路。
        """
        try:
            token = self._get_x_bearer_token(feed)
            if not token:
                error = "缺少 X API Bearer Token（请在该 feed 配置 bearer_token 或设置环境变量 X_BEARER_TOKEN）"
                print(f"[RSS] {feed.name}: {error}")
                return [], error

            # 解析 username
            username = feed.url[len("x://") :].strip().lstrip("@").strip()
            if not username:
                error = "x:// 协议缺少用户名（示例：x://elonmusk）"
                print(f"[RSS] {feed.name}: {error}")
                return [], error

            # 1) 先通过用户名获取用户 ID
            user_resp = self._x_api_get(
                token=token,
                path=f"/2/users/by/username/{username}",
                params={"user.fields": "username,name"},
            )
            if user_resp.status_code == 429:
                error = "X API 限流（HTTP 429）"
                print(f"[RSS] {feed.name}: {error}")
                return [], error
            user_resp.raise_for_status()
            user_json = user_resp.json() if user_resp.content else {}
            user_data = user_json.get("data") or {}
            user_id = user_data.get("id")
            if not user_id:
                error = f"无法解析用户ID（username={username}）"
                print(f"[RSS] {feed.name}: {error}")
                return [], error

            # 2) 拉取该用户最新推文
            max_items = feed.max_items if feed.max_items and feed.max_items > 0 else 20
            max_items = max(5, min(int(max_items), 100))

            tweets_resp = self._x_api_get(
                token=token,
                path=f"/2/users/{user_id}/tweets",
                params={
                    "max_results": str(max_items),
                    "tweet.fields": "created_at,lang",
                    "exclude": "replies,retweets",
                },
            )
            if tweets_resp.status_code == 429:
                error = "X API 限流（HTTP 429）"
                print(f"[RSS] {feed.name}: {error}")
                return [], error
            tweets_resp.raise_for_status()
            tweets_json = tweets_resp.json() if tweets_resp.content else {}
            tweets = tweets_json.get("data") or []

            now = get_configured_time(self.timezone)
            crawl_time = now.strftime("%H:%M")

            items: List[RSSItem] = []
            for t in tweets:
                tweet_id = (t.get("id") or "").strip()
                text = (t.get("text") or "").strip()
                if not tweet_id or not text:
                    continue

                # published_at：尽量转换 Z -> +00:00，保证后续 is_within_days 可解析
                created_at = (t.get("created_at") or "").strip()
                if created_at.endswith("Z"):
                    created_at = created_at.replace("Z", "+00:00")

                url = f"https://x.com/{username}/status/{tweet_id}"
                title = text
                if len(title) > 120:
                    title = title[:120] + "..."

                item = RSSItem(
                    title=title,
                    feed_id=feed.id,
                    feed_name=feed.name,
                    url=url,
                    published_at=created_at,
                    summary=text,
                    author=f"@{username}",
                    crawl_time=crawl_time,
                    first_time=crawl_time,
                    last_time=crawl_time,
                    count=1,
                )
                items.append(item)

            print(f"[RSS] {feed.name}: 获取 {len(items)} 条（X 用户 @{username}）")
            return items, None

        except requests.Timeout:
            error = f"请求超时 ({self.timeout}s)"
            print(f"[RSS] {feed.name}: {error}")
            return [], error
        except requests.RequestException as e:
            error = f"请求失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error
        except ValueError as e:
            error = f"解析失败: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error
        except Exception as e:
            error = f"未知错误: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

    def fetch_all(self) -> RSSData:
        """
        抓取所有 RSS 源

        Returns:
            RSSData 对象
        """
        all_items: Dict[str, List[RSSItem]] = {}
        id_to_name: Dict[str, str] = {}
        failed_ids: List[str] = []

        # 使用配置的时区
        now = get_configured_time(self.timezone)
        crawl_time = now.strftime("%H:%M")
        crawl_date = now.strftime("%Y-%m-%d")

        print(f"[RSS] 开始抓取 {len(self.feeds)} 个 RSS 源...")

        for i, feed in enumerate(self.feeds):
            # 请求间隔（带随机波动）
            if i > 0:
                interval = self.request_interval / 1000
                jitter = random.uniform(-0.2, 0.2) * interval
                time.sleep(interval + jitter)

            items, error = self.fetch_feed(feed)

            id_to_name[feed.id] = feed.name

            if error:
                failed_ids.append(feed.id)
            else:
                all_items[feed.id] = items

        total_items = sum(len(items) for items in all_items.values())
        print(f"[RSS] 抓取完成: {len(all_items)} 个源成功, {len(failed_ids)} 个失败, 共 {total_items} 条")

        return RSSData(
            date=crawl_date,
            crawl_time=crawl_time,
            items=all_items,
            id_to_name=id_to_name,
            failed_ids=failed_ids,
        )

    @classmethod
    def from_config(cls, config: Dict) -> "RSSFetcher":
        """
        从配置字典创建抓取器

        Args:
            config: 配置字典，格式如下：
                {
                    "enabled": true,
                    "request_interval": 2000,
                    "freshness_filter": {
                        "enabled": true,
                        "max_age_days": 3
                    },
                    "feeds": [
                        {"id": "hacker-news", "name": "Hacker News", "url": "...", "max_age_days": 1}
                    ]
                }

        Returns:
            RSSFetcher 实例
        """
        # 读取新鲜度过滤配置
        freshness_config = config.get("freshness_filter", {})
        freshness_enabled = freshness_config.get("enabled", True)  # 默认启用
        default_max_age_days = freshness_config.get("max_age_days", 3)  # 默认3天

        feeds = []
        for feed_config in config.get("feeds", []):
            # 读取并验证单个 feed 的 max_age_days（可选）
            max_age_days_raw = feed_config.get("max_age_days")
            max_age_days = None
            if max_age_days_raw is not None:
                try:
                    max_age_days = int(max_age_days_raw)
                    if max_age_days < 0:
                        feed_id = feed_config.get("id", "unknown")
                        print(f"[警告] RSS feed '{feed_id}' 的 max_age_days 为负数，将使用全局默认值")
                        max_age_days = None
                except (ValueError, TypeError):
                    feed_id = feed_config.get("id", "unknown")
                    print(f"[警告] RSS feed '{feed_id}' 的 max_age_days 格式错误：{max_age_days_raw}")
                    max_age_days = None

            feed = RSSFeedConfig(
                id=feed_config.get("id", ""),
                name=feed_config.get("name", ""),
                url=feed_config.get("url", ""),
                bearer_token=feed_config.get("bearer_token"),
                max_items=feed_config.get("max_items", 0),  # 0=不限制
                enabled=feed_config.get("enabled", True),
                max_age_days=max_age_days,  # None=使用全局，0=禁用，>0=覆盖
            )
            if feed.id and feed.url:
                feeds.append(feed)

        return cls(
            feeds=feeds,
            request_interval=config.get("request_interval", 2000),
            timeout=config.get("timeout", 15),
            use_proxy=config.get("use_proxy", False),
            proxy_url=config.get("proxy_url", ""),
            timezone=config.get("timezone", DEFAULT_TIMEZONE),
            freshness_enabled=freshness_enabled,
            default_max_age_days=default_max_age_days,
        )
