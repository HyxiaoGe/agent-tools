"""给 TrendRadar 官方 MCP 补齐调用契约后再启动，上游代码不改。

- 上游参数定义里枚举类参数只写了 string，可选值只在说明文字里，模型常猜错（mode="weibo"、
  sort_by="time"）。列工具时把可选值写进 enum，平台 ID 按 config.yaml 当前配置实时给出。
- 上游出错时返回 {"success": false, "error": {...}} 却不标记 isError，调用方会当成功。
  这里改为工具错误，错误码和建议原样带回，调用方才能如实记为失败。
"""

import copy
import json
import os
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp_server import server as upstream
from mcp_server.utils.validators import get_supported_platforms

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


def with_enums(
    tool_name: str, parameters: dict[str, Any], platforms: list[str]
) -> dict[str, Any]:
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
        if (
            isinstance(payload, dict)
            and payload.get("success") is False
            and isinstance(payload.get("error"), dict)
        ):
            return payload["error"]
    return None


class TrendsContract(Middleware):
    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        platforms = get_supported_platforms()
        return [
            tool.model_copy(
                update={"parameters": with_enums(tool.name, tool.parameters, platforms)}
            )
            for tool in tools
        ]

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        error = upstream_error(result.content)
        if error is not None:
            raise ToolError(json.dumps(error, ensure_ascii=False))
        return result


if __name__ == "__main__":
    upstream.mcp.add_middleware(TrendsContract())
    upstream.run_server(
        transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "3333"))
    )
