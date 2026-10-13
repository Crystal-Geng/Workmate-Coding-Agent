from __future__ import annotations

import json
from pathlib import Path

from evals.runners.sandbox_tools import (
    SandboxedBashTool,
    SandboxedReadFileTool,
    SandboxedWriteFileTool,
)
from evals.sandbox import landlock_abi


async def verify_isolation(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    private = root / "evaluator-private"
    other = root / "other-trial"
    for path in (workspace, private, other):
        path.mkdir(parents=True, exist_ok=False)
    (workspace / "task.txt").write_text("task-visible\n")
    (private / "sentinel.txt").write_text("PRIVATE-SENTINEL\n")
    (other / "answer.txt").write_text("OTHER-BRANCH-SENTINEL\n")
    (workspace / "escape-link").symlink_to(private / "sentinel.txt")

    reader = SandboxedReadFileTool(workspace)
    writer = SandboxedWriteFileTool(workspace)
    shell = SandboxedBashTool(workspace)
    checks: dict[str, bool] = {}

    checks["normal_read"] = not (await reader.invoke({"path": "task.txt"})).is_error
    checks["normal_write"] = not (
        await writer.invoke({"path": "output/result.txt", "content": "written\n"})
    ).is_error
    checks["normal_command"] = not (
        await shell.invoke({"command": "python -c \"open('command.txt','w').write('ok')\""})
    ).is_error
    for name, path in {
        "file_absolute": private / "sentinel.txt",
        "file_parent": "../evaluator-private/sentinel.txt",
        "symlink_escape": "escape-link",
        "other_trial": other / "answer.txt",
    }.items():
        result = await reader.invoke({"path": str(path)})
        checks[name] = result.is_error and "SENTINEL" not in result.content

    commands = {
        "shell_private": f"cat {private / 'sentinel.txt'}",
        "python_private": f"python -c \"print(open('{private / 'sentinel.txt'}').read())\"",
        "proc_parent": "cat /proc/$PPID/environ",
        "environment_secret": 'test -z "$ANTHROPIC_API_KEY"',
        "tcp_network": 'python -c "import socket; socket.socket()"',
        "udp_network": 'python -c "import socket; socket.socket(type=socket.SOCK_DGRAM)"',
    }
    for name, command in commands.items():
        result = await shell.invoke({"command": command})
        expected_success = name == "environment_secret"
        checks[name] = (not result.is_error) if expected_success else result.is_error
        checks[name] = checks[name] and "SENTINEL" not in result.content

    evidence = {
        "mechanism": "Landlock ABI 4 filesystem/TCP rules plus x86_64 seccomp",
        "landlock_abi": landlock_abi(),
        "network_policy": "all task-tool socket syscalls denied; provider remains in trusted runner",  # noqa: E501
        "external_tools": "no RAG or host MCP registered; synthetic local MCP is explicit per case",
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
        "limitations": [
            "This verifies the listed attack surfaces, not absence of every kernel escape.",
            "The sandbox syscall table currently supports x86_64 Linux only.",
        ],
    }
    (root / "isolation.json").write_text(json.dumps(evidence, indent=2))
    if not evidence["passed"]:
        failed = ", ".join(name for name, passed in checks.items() if not passed)
        raise RuntimeError(f"OS isolation self-test failed: {failed}")
    return evidence
