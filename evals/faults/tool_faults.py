from pydantic import ValidationError

from evals.faults.base import FaultState
from workmate.core.tools.base import BaseTool, ToolResult
from workmate.core.tools.errors import RateLimitedError


class ToolFault(BaseTool):
    # 保持真实工具的描述与 schema，并包装故障边界
    def __init__(self, inner: BaseTool, state: FaultState):
        self.inner, self.state = inner, state
        self.name, self.description = inner.name, inner.description
        self.input_schema = inner.input_schema
        self.params_model = inner.params_model
        if state.kind == "schema_error":
            original = inner.params_model

            class OnceValidation:
                @classmethod
                # 在 invoke_tool 的真实参数校验位置注入一次 Pydantic 错误
                def model_validate(cls, params):
                    if state.hit():
                        raise ValidationError.from_exception_data(
                            "InjectedParams",
                            [{"type": "missing", "loc": ("injected_required",), "input": params}],
                        )
                    return original.model_validate(params) if original else params

            self.params_model = OnceValidation

    # 在真实 invoke 内注入错误，让生产 invoke_tool 决定是否重试
    async def invoke(self, params):
        if self.state.kind != "schema_error" and self.state.hit():
            if self.state.kind == "rate_limited":
                raise RateLimitedError("eval: one-shot rate limit")
            if self.state.kind == "timeout":
                raise TimeoutError("eval: synthetic timeout at invoke boundary")
            return ToolResult("eval: one-shot runtime error", True, "runtime_error")
        result = await self.inner.invoke(params)
        if not result.is_error:
            self.state.complete()
        return result
