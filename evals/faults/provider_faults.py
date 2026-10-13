from types import SimpleNamespace

import httpx

from evals.faults.base import FaultState


class StreamContext:
    # 包装 SDK stream 上下文，保持异常位于 Provider 内部捕获范围
    def __init__(self, inner, state: FaultState):
        self.inner, self.state = inner, state
        self.stream = None

    # 进入 SDK 流
    async def __aenter__(self):
        self.stream = await self.inner.__aenter__()
        return self

    # 委托 SDK 释放连接
    async def __aexit__(self, *args):
        return await self.inner.__aexit__(*args)

    @property
    # 返回会在中途触发故障的底层迭代器
    def text_stream(self):
        return self.iterate()

    # 在首个文本片段后抛出生产 Provider 支持的网络错误
    async def iterate(self):
        first = True
        async for text in self.stream.text_stream:
            yield text
            if first:
                first = False
                if self.state.hit():
                    raise httpx.RemoteProtocolError("eval: disconnected after text chunk")

    # 读取 SDK 最终消息
    async def get_final_message(self):
        result = await self.stream.get_final_message()
        if self.state.injections:
            self.state.complete()
        return result


class StreamClient:
    # 只替换 SDK messages.stream，保留 AnthropicProvider.chat 的真实重试循环
    def __init__(self, client, state: FaultState):
        self.client, self.state = client, state
        self.messages = SimpleNamespace(stream=self.stream)

    # 为每次 SDK 请求创建流包装
    def stream(self, **kwargs):
        return StreamContext(self.client.messages.stream(**kwargs), self.state)
