import asyncio
import json
import socket
import sys
from pathlib import Path

from workmate.core.transport.socket_client import SocketClient


# 创建独立 IPC 客户端及其事件接收任务
async def connect(port):
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    task = asyncio.create_task(client.run_event_loop())
    return client, task


# 在独立子进程运行真实 CoreApp，通过生产 SocketClient 完成审批或重连
async def run(request_path, case):
    request = json.loads(request_path.read_text())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        request["port"] = listener.getsockname()[1]
    daemon_request = request_path.with_name("evaluator-private-daemon-request.json")
    daemon_request.write_text(json.dumps(request))
    log = request_path.with_name("daemon.log").open("w")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "evals.runners.worker",
        str(daemon_request),
        "--daemon",
        stdout=log,
        stderr=log,
    )
    connections = []
    evidence = {"reconnected": False, "replayed_count": 0}
    received = []
    started = asyncio.Event()
    finished = asyncio.Event()
    expected_run = None
    try:
        for _ in range(200):
            if proc.returncode is not None:
                raise RuntimeError("evaluation daemon exited during startup; see daemon.log")
            try:
                command, task = await connect(request["port"])
                connections.append((command, task))
                break
            except OSError:
                await asyncio.sleep(0.025)
        else:
            raise RuntimeError("evaluation daemon startup timeout")
        approvals, task = await connect(request["port"])
        connections.append((approvals, task))
        events, event_task = await connect(request["port"])
        connections.append((events, event_task))

        # 通过独立审批连接响应生产权限事件，避免接收循环自锁
        async def on_event(event):
            received.append(event)
            if event["type"] == "run.started":
                started.set()
            if event["type"] == "run.finished":
                if expected_run is None or event["run_id"] == expected_run:
                    finished.set()
            if event["type"] == "permission.requested":
                decision = "allow_once"
                if case.permission == "deny_outside":
                    params = event.get("params", {})
                    path = str(params.get("path", ""))
                    if ".." in Path(path).parts or Path(path).is_absolute():
                        decision = "deny_once"
                    if event.get("tool_name") == "bash":
                        decision = "deny_once"
                await approvals.send_command(
                    "permission.respond",
                    {"tool_use_id": event["tool_use_id"], "decision": decision},
                )

        events.on_event(on_event)
        await events.send_command("event.subscribe", {"topics": ["*"], "scope": "global"})
        if case.reconnect:
            reply = await command.send_command("agent.run", {"goal": case.goal})
            expected_run = reply["run_id"]
            await started.wait()
            evidence["disconnected_before_finish"] = not finished.is_set()
            await events.close()
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
            # 完成期间允许 daemon 继续执行，重连时只订阅原 run，绝不重发目标
            events, task = await connect(request["port"])
            connections.append((events, task))
            events.on_event(on_event)
            reply = await events.send_command(
                "event.subscribe",
                {"topics": ["*"], "scope": "global", "replay_from_run": expected_run},
            )
            evidence.update(
                reconnected=True,
                replayed_count=reply.get("replayed_count", 0),
                replay_run_id=expected_run,
                submitted_runs=1,
            )
            await finished.wait()
        else:
            reply = await command.send_command("session.create", {"mode": "chat"})
            for goal in case.turns + [case.goal]:
                await command.send_command(
                    "session.send_message", {"session_id": reply["session_id"], "content": goal}
                )
        return evidence
    finally:
        for client, task in connections:
            await client.close()
            task.cancel()
        await asyncio.gather(*(t for _, t in connections), return_exceptions=True)
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        log.close()
