"""Minimal real MCP stdio transport using bounded JSON-RPC messages."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from lhas.execution_control import ExecutionControlError, ExecutionControlToken, await_with_control
from lhas.mcp.models import MCPServerConfig, MCPToolInfo

MAX_MCP_MESSAGE = 256_000


@dataclass
class _Connection:
    config: MCPServerConfig
    process: asyncio.subprocess.Process
    lock: asyncio.Lock
    execution_control: ExecutionControlToken | None = None
    next_id: int = 1


class MCPManager:
    def __init__(self):
        self._connections: dict[str, _Connection] = {}
        self._tools: dict[str, MCPToolInfo] = {}

    async def connect(
        self,
        config: MCPServerConfig,
        *,
        execution_control: ExecutionControlToken | None = None,
    ) -> list[MCPToolInfo]:
        if config.name in self._connections:
            raise ValueError(f"MCP server already connected: {config.name}")
        if execution_control is not None:
            execution_control.check()
        env = {key: value for key, value in os.environ.items() if "KEY" not in key.upper() and "TOKEN" not in key.upper() and "SECRET" not in key.upper()}
        env.update(config.env)
        process = await await_with_control(
            asyncio.create_subprocess_exec(
                *config.command,
                cwd=config.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            control=execution_control,
            local_ceiling=config.timeout_seconds,
            timeout_failure_type="MCP_TIMEOUT",
            source="mcp_startup",
        )
        connection = _Connection(
            config=config,
            process=process,
            lock=asyncio.Lock(),
            execution_control=execution_control,
        )
        self._connections[config.name] = connection
        try:
            await self._request(
                config.name,
                "initialize",
                {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"odys","version":"0.1.0"}},
                execution_control=execution_control,
            )
            await self._notify(
                config.name,
                "notifications/initialized",
                {},
                execution_control=execution_control,
            )
            return await self.discover(config.name)
        except Exception:
            await self.close(config.name)
            raise

    async def discover(self, server_name: str) -> list[MCPToolInfo]:
        payload = await self._request(server_name, "tools/list", {})
        discovered: list[MCPToolInfo] = []
        for item in list(payload.get("tools", []))[:200]:
            remote_name = str(item.get("name", ""))[:128]
            if not remote_name:
                continue
            capability = f"mcp.{server_name}.{remote_name}"
            info = MCPToolInfo(
                name=capability,
                description=str(item.get("description", ""))[:2_000],
                input_schema=dict(item.get("inputSchema", {})),
                server_name=server_name,
            )
            self._tools[capability] = info
            discovered.append(info)
        return discovered

    def list_servers(self) -> list[str]:
        return sorted(self._connections)

    def list_tools(self) -> list[MCPToolInfo]:
        return [self._tools[name] for name in sorted(self._tools)]

    async def call_tool(
        self,
        capability: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        execution_control: ExecutionControlToken | None = None,
    ) -> dict[str, Any]:
        try:
            info = self._tools[capability]
        except KeyError as exc:
            raise KeyError(f"unknown MCP capability: {capability}") from exc
        remote_name = capability.removeprefix(f"mcp.{info.server_name}.")
        result = await self._request(
            info.server_name,
            "tools/call",
            {"name":remote_name,"arguments":arguments},
            timeout_seconds=timeout_seconds,
            execution_control=execution_control,
        )
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded) > MAX_MCP_MESSAGE:
            return {"content": encoded[:MAX_MCP_MESSAGE], "truncated": True}
        return result

    async def _notify(
        self,
        server_name: str,
        method: str,
        params: dict[str, Any],
        *,
        execution_control: ExecutionControlToken | None = None,
    ) -> None:
        connection = self._connections[server_name]
        control = execution_control or connection.execution_control
        if control is not None:
            control.check()
        payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params}, separators=(",",":")) + "\n"
        connection.process.stdin.write(payload.encode("utf-8"))
        await await_with_control(
            connection.process.stdin.drain(),
            control=control,
            local_ceiling=connection.config.timeout_seconds,
            timeout_failure_type="MCP_TIMEOUT",
            source="mcp_notify",
        )

    async def _request(
        self,
        server_name: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        execution_control: ExecutionControlToken | None = None,
    ) -> dict[str, Any]:
        connection = self._connections[server_name]
        control = execution_control or connection.execution_control
        async with connection.lock:
            if control is not None:
                control.check()
            request_id = connection.next_id
            connection.next_id += 1
            payload = json.dumps({"jsonrpc":"2.0","id":request_id,"method":method,"params":params}, separators=(",",":")) + "\n"
            if len(payload) > MAX_MCP_MESSAGE:
                raise ValueError("MCP_REQUEST_TOO_LARGE")
            connection.process.stdin.write(payload.encode("utf-8"))
            try:
                await await_with_control(
                    connection.process.stdin.drain(),
                    control=control,
                    local_ceiling=timeout_seconds or connection.config.timeout_seconds,
                    timeout_failure_type="MCP_TIMEOUT",
                    source="mcp_request",
                )
                while True:
                    raw = await await_with_control(
                        connection.process.stdout.readline(),
                        control=control,
                        local_ceiling=timeout_seconds or connection.config.timeout_seconds,
                        timeout_failure_type="MCP_TIMEOUT",
                        source="mcp_request",
                    )
                    if not raw:
                        raise RuntimeError("MCP_SERVER_CLOSED")
                    if len(raw) > MAX_MCP_MESSAGE:
                        raise RuntimeError("MCP_RESPONSE_TOO_LARGE")
                    message = json.loads(raw.decode("utf-8"))
                    if message.get("id") != request_id:
                        continue
                    if "error" in message:
                        error = message["error"]
                        raise RuntimeError(f"MCP_ERROR_{error.get('code','UNKNOWN')}")
                    return dict(message.get("result", {}))
            except ExecutionControlError:
                _terminate_process_now(connection.process)
                raise

    async def close(self, server_name: str) -> None:
        connection = self._connections.pop(server_name, None)
        if connection is None:
            return
        for capability in [name for name, item in self._tools.items() if item.server_name == server_name]:
            self._tools.pop(capability, None)
        if connection.process.returncode is None:
            connection.process.terminate()
            try:
                await asyncio.wait_for(connection.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                connection.process.kill()
                await connection.process.wait()

    async def close_all(self) -> None:
        for name in list(self._connections):
            await self.close(name)


def _terminate_process_now(process: asyncio.subprocess.Process) -> None:
    """Best-effort transport cancellation; ``close`` remains cleanup owner."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
