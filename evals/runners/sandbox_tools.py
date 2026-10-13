from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from workmate.core.subagent.tool import SpawnAgentTool
from workmate.core.tools.base import ToolResult
from workmate.core.tools.builtin.bash import BashParams, BashTool
from workmate.core.tools.builtin.list_dir import ListDirParams, ListDirTool
from workmate.core.tools.builtin.read_file import ReadFileParams, ReadFileTool
from workmate.core.tools.builtin.write_file import WriteFileParams, WriteFileTool

_MAX_OUTPUT = 64 * 1024
_SANDBOX = Path(__file__).parents[1] / "sandbox.py"


class _SandboxMixin:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    async def _run(self, operation: str, payload: dict[str, object]):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_SANDBOX),
            "--workspace",
            str(self.workspace),
            operation,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.workspace,
        )
        stdout, _ = await proc.communicate(json.dumps(payload).encode())
        text = stdout.decode("utf-8", errors="replace")
        if len(stdout) > _MAX_OUTPUT:
            text = text[:_MAX_OUTPUT] + "\n[truncated]"
        return proc.returncode or 0, text


class SandboxedReadFileTool(_SandboxMixin, ReadFileTool):
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        code, output = await self._run("read", ReadFileParams.model_validate(params).model_dump())
        return ToolResult(
            content=output, is_error=bool(code), error_type="runtime_error" if code else None
        )


class SandboxedWriteFileTool(_SandboxMixin, WriteFileTool):
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        code, output = await self._run("write", WriteFileParams.model_validate(params).model_dump())
        return ToolResult(
            content=output, is_error=bool(code), error_type="runtime_error" if code else None
        )


class SandboxedListDirTool(_SandboxMixin, ListDirTool):
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        code, output = await self._run("list", ListDirParams.model_validate(params).model_dump())
        return ToolResult(
            content=output, is_error=bool(code), error_type="runtime_error" if code else None
        )


class SandboxedBashTool(_SandboxMixin, BashTool):
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = BashParams.model_validate(params)
        command = [
            sys.executable,
            str(_SANDBOX),
            "--workspace",
            str(self.workspace),
            "exec",
            parsed.command,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.workspace,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), parsed.timeout)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {parsed.timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        output = stdout.decode("utf-8", errors="replace")
        if len(stdout) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n[truncated]"
        if proc.returncode:
            return ToolResult(
                content=f"[exit {proc.returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")


def install_sandbox_tools(registry, workspace: Path) -> None:
    for tool in (
        SandboxedReadFileTool(workspace),
        SandboxedBashTool(workspace),
        SandboxedWriteFileTool(workspace),
        SandboxedListDirTool(workspace),
    ):
        registry.register(tool)


class SandboxedSpawnAgentTool(SpawnAgentTool):
    def __init__(self, *args, workspace: Path, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._workspace = workspace

    def _build_child_registry(self, child_bus, child_run_id, profile):
        registry = super()._build_child_registry(child_bus, child_run_id, profile)
        install_sandbox_tools(registry, self._workspace)
        nested = registry.get("spawn_agent")
        if nested:
            registry.register(
                SandboxedSpawnAgentTool(
                    provider=nested._provider,
                    parent_bus=nested._parent_bus,
                    parent_run_id=nested._parent_run_id,
                    permission_manager=nested._permission_manager,
                    max_steps=nested._max_steps,
                    task_registry=nested._task_registry,
                    runs_dir=nested._runs_dir,
                    session_id=nested._session_id,
                    depth=nested._depth,
                    workspace=self._workspace,
                )
            )
        return registry
