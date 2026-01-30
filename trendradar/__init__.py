# coding=utf-8
"""
TrendRadar - 热点新闻聚合与分析工具

使用方式:
  python -m trendradar        # 模块执行
  trendradar                  # 安装后执行
"""

__version__ = "5.5.0"
__all__ = ["AppContext", "__version__"]


def __getattr__(name: str):
    """
    延迟导入，避免 import trendradar 时拉起重依赖（如 litellm）。
    """
    if name == "AppContext":
        from trendradar.context import AppContext  # pylint: disable=import-error
        return AppContext
    raise AttributeError(name)
