"""给 TrendRadar 官方 MCP 补齐调用契约后再启动，上游代码不改。

- 上游参数定义里枚举类参数只写了 string，可选值只在说明文字里，模型常猜错（mode="weibo"、
  sort_by="time"）。列工具时把可选值写进 enum，平台 ID 按 config.yaml 当前配置实时给出。
- 上游出错时返回 {"success": false, "error": {...}} 却不标记 isError，调用方会当成功。
  这里改为工具错误，错误码和建议原样带回，调用方才能如实记为失败。
- 上游 get_latest_news 取的不是最新一批（当天所有上过榜的标题、第一次上榜的名次），
  取数据的那一步换成 latest_news.py；参数校验、返回格式仍走上游的工具外层。
- 只开放查询类工具。网关按服务放行、不分工具，上游的发通知、触发抓取这类工具不能交给调用方自己挡。
"""

import asyncio
import copy
import json
import os
from datetime import datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp_server import server as upstream
from mcp_server.services.data_service import DataService
from mcp_server.utils.errors import DataNotFoundError
from mcp_server.utils.validators import get_supported_platforms

from latest_news import latest_news

# 只开放这 16 个查询类工具，都只读 TrendRadar 抓下来的本地数据（分析类只用上游算权重的函数，不调模型、不联网）。
# 上游另外 11 个：发通知、查通知渠道和格式（send_notification 等）、立刻抓取、从远程同步、经 r.jina.ai 读任意网页、
# 看配置、系统和存储状态、查版本。网关只按服务放行，任何能调 trends 的产品都看得到、调得到，
# 和抓取端共用一份配置，哪天配上推送渠道，send_notification 就真能往群里发。
# 启动时删掉：不出现在列表里，点名调用回的是 Unknown tool，和不存在一样。上游升级加的新工具默认也不开放，
# 要开放得先确认是只读的，再加进这里。
QUERY_TOOLS = frozenset(
    {
        "resolve_date_range",
        "get_latest_news",
        "get_trending_topics",
        "get_news_by_date",
        "search_news",
        "find_related_news",
        "aggregate_news",
        "analyze_topic_trend",
        "analyze_data_insights",
        "analyze_sentiment",
        "compare_periods",
        "generate_summary_report",
        "get_latest_rss",
        "search_rss",
        "get_rss_feeds_status",
        "list_available_dates",
    }
)

# 取值与上游工具内部校验一致：data_query.py / search_tools.py / analytics.py。
# 升级镜像版本时要重新核对。
PARAM_ENUMS: dict[str, dict[str, list[str]]] = {
    "get_trending_topics": {
        "mode": ["daily", "current"],
        "extract_mode": ["keywords", "auto_extract"],
    },
    "search_news": {
        "search_mode": ["keyword", "fuzzy", "entity"],
        "sort_by": ["relevance", "weight", "date"],
    },
    "analyze_topic_trend": {
        "analysis_type": ["trend", "lifecycle", "viral", "predict"],
    },
}


def with_enums(tool_name: str, parameters: dict[str, Any], platforms: list[str]) -> dict[str, Any]:
    schema = copy.deepcopy(parameters)
    properties = schema.get("properties", {})
    for name, values in PARAM_ENUMS.get(tool_name, {}).items():
        if name in properties:
            properties[name]["enum"] = values
    # 平台列表读取失败时上游不校验平台，这里也不加限制。
    if platforms and "platforms" in properties:
        platform_schema = properties["platforms"]
        for branch in platform_schema.get("anyOf", [platform_schema]):
            if branch.get("type") == "array":
                branch["items"] = {**branch.get("items", {}), "enum": platforms}
    return schema


def upstream_error(content: list[Any]) -> dict[str, Any] | None:
    for block in content:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("success") is False and isinstance(payload.get("error"), dict):
            return payload["error"]
    return None


def get_latest_news(
    self: DataService,
    platforms: list[str] | None = None,
    limit: int = 50,
    include_url: bool = False,
) -> list[dict]:
    """替换上游的 DataService.get_latest_news，见 latest_news.py。"""
    items = latest_news(
        lambda day: self.parser._get_db_path(day, "news"),
        datetime.now(),  # 容器 TZ=Asia/Shanghai，和抓取端按北京时间分库一致
        platforms,
        limit,
        include_url,
    )
    if not items:
        raise DataNotFoundError("今天和昨天都没有热榜数据", suggestion="检查 TrendRadar 的抓取是否在运行")
    return items


async def keep_query_tools(server: FastMCP) -> list[str]:
    """删掉不在 QUERY_TOOLS 里的工具，返回删掉的名字。"""
    removed = sorted(set(await server.get_tools()) - QUERY_TOOLS)
    for name in removed:
        server.remove_tool(name)
    return removed


class TrendsContract(Middleware):
    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        platforms = get_supported_platforms()
        return [
            tool.model_copy(update={"parameters": with_enums(tool.name, tool.parameters, platforms)}) for tool in tools
        ]

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        error = upstream_error(result.content)
        if error is not None:
            raise ToolError(json.dumps(error, ensure_ascii=False))
        return result


if __name__ == "__main__":
    DataService.get_latest_news = get_latest_news
    removed = asyncio.run(keep_query_tools(upstream.mcp))
    print(f"只开放查询类工具，去掉 {len(removed)} 个：{', '.join(removed)}", flush=True)
    upstream.mcp.add_middleware(TrendsContract())
    upstream.run_server(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "3333")))
