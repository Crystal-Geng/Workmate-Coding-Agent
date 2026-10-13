import json
import math
from typing import Protocol


class TokenCounter(Protocol):
    method: str

    # 对同一完整请求表示计算 token
    def count(self, request: dict) -> int: ...


class EstimatedTokenCounter:
    method = "estimated:utf8-json-bytes/4:v1"

    # 对系统提示、消息及工具 schema 使用固定序列化与同一估算器
    def count(self, request: dict) -> int:
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return math.ceil(len(raw.encode("utf-8")) / 4)


# 构造与本项目 AnthropicProvider 一致的完整请求计数范围
def request_shape(messages, system, tools, model):
    schemas = [dict(t) for t in tools]
    if schemas:
        schemas[-1]["cache_control"] = {"type": "ephemeral"}
    request = {
        "model": model,
        "max_tokens": 8192,
        "messages": messages,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
    }
    if schemas:
        request["tools"] = schemas
    return request
