"""Run the v2.4 stdio/Streamable HTTP MCP transport acceptance probe."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb
import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_servers.common.contracts import MCPRequestContext, stable_hash
from mcp_servers.common.sdk_adapter import MCP_PROTOCOL_VERSION
from mcp_servers.dataset_ops.tools import aggregate_preview
from packages.common.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "stage0-local-probe-token"


@dataclass(frozen=True, slots=True)
class TransportResult:
    name: str
    result_hash: str
    protocol_version: str
    tool_count: int
    latency_ms: float
    error_codes: dict[str, str]
    session_created: bool
    graceful_close: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "result_hash": self.result_hash,
            "protocol_version": self.protocol_version,
            "tool_count": self.tool_count,
            "latency_ms": round(self.latency_ms, 3),
            "error_codes": self.error_codes,
            "session_created": self.session_created,
            "graceful_close": self.graceful_close,
        }


def _context(*, expired: bool = False) -> MCPRequestContext:
    deadline = datetime.now(UTC) + (
        timedelta(seconds=-1) if expired else timedelta(minutes=1)
    )
    return MCPRequestContext(
        subject_id="probe-user",
        project_id="probe-project",
        conversation_id="probe-conversation",
        run_id="probe-run",
        plan_version=0,
        step_id="probe-step",
        invocation_id="probe-invocation",
        idempotency_key="probe-idempotency",
        permission_snapshot_id="probe-permissions",
        trace_id="probe-trace",
        deadline_at=deadline.isoformat(),
    )


def _write_dataset(directory: Path) -> tuple[str, dict[str, Any]]:
    dataset_ref = "stage0_mcp_probe"
    path = directory / f"{dataset_ref}.parquet"
    connection = duckdb.connect()
    try:
        connection.execute(
            """
            COPY (
                SELECT * FROM (
                    VALUES ('east', 10), ('east', 15), ('west', 8)
                ) AS probe(region, amount)
            ) TO ? (FORMAT PARQUET)
            """,
            [str(path)],
        )
    finally:
        connection.close()
    arguments = {
        "dataset_ref": dataset_ref,
        "group_col": "region",
        "value_col": "amount",
        "agg": "sum",
        "sort": "group",
    }
    return dataset_ref, arguments


def _error_code(result: Any) -> str:
    meta = result.meta or {}
    code = meta.get("com.chatbi/error-code")
    return str(code) if code else "missing"


async def _exercise_session(
    session: ClientSession,
    *,
    name: str,
    arguments: dict[str, Any],
    session_created: bool,
) -> TransportResult:
    started = time.perf_counter()
    initialized = await session.initialize()
    listed = await session.list_tools()
    names = [tool.name for tool in listed.tools]
    if names != ["aggregate_preview"]:
        raise RuntimeError(f"{name} 工具发现不一致: {names}")
    success = await session.call_tool(
        "aggregate_preview",
        arguments,
        meta=_context().to_request_meta(),
    )
    if success.isError or not isinstance(success.structuredContent, dict):
        raise RuntimeError(f"{name} 合法调用失败")
    invalid = await session.call_tool(
        "aggregate_preview",
        {**arguments, "agg": "median"},
        meta=_context().to_request_meta(),
    )
    unknown = await session.call_tool(
        "missing_tool",
        {},
        meta=_context().to_request_meta(),
    )
    business = await session.call_tool(
        "aggregate_preview",
        {
            "dataset_ref": arguments["dataset_ref"],
            "group_col": "region",
            "agg": "sum",
        },
        meta=_context().to_request_meta(),
    )
    expired = await session.call_tool(
        "aggregate_preview",
        arguments,
        meta=_context(expired=True).to_request_meta(),
    )
    errors = {
        "schema": _error_code(invalid),
        "unknown_tool": _error_code(unknown),
        "business": _error_code(business),
        "deadline": _error_code(expired),
    }
    expected = {
        "schema": "invalid_arguments",
        "unknown_tool": "tool_not_found",
        "business": "tool_business_error",
        "deadline": "deadline_exceeded",
    }
    if errors != expected:
        raise RuntimeError(f"{name} 异常映射不一致: {errors}")
    return TransportResult(
        name=name,
        result_hash=stable_hash(success.structuredContent),
        protocol_version=str(initialized.protocolVersion),
        tool_count=len(listed.tools),
        latency_ms=(time.perf_counter() - started) * 1000,
        error_codes=errors,
        session_created=session_created,
        graceful_close=True,
    )


async def _probe_stdio(
    dataset_dir: Path,
    arguments: dict[str, Any],
) -> TransportResult:
    env = {
        "DATASET_DIR": str(dataset_dir),
        "MCP_TRANSPORT": "stdio",
        "MCP_PROBE_DELAY_SECONDS": "0",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "scripts.mcp_transport_probe_server"],
        env=env,
        cwd=ROOT,
    )
    with tempfile.TemporaryFile(mode="w+") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                return await _exercise_session(
                    session,
                    name="stdio",
                    arguments=arguments,
                    session_created=False,
                )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_http(url: str, process: asyncio.subprocess.Process) -> None:
    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
        for _ in range(400):
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b""
                tail = stderr.decode(errors="replace")[-1000:]
                raise RuntimeError(
                    f"Streamable HTTP probe server 提前退出: {tail}"
                )
            try:
                response = await client.get(url)
                if response.status_code == 401:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError("Streamable HTTP probe server 未就绪")


def _initialize_payload(protocol: str = MCP_PROTOCOL_VERSION) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol,
            "capabilities": {},
            "clientInfo": {"name": "chatbi-probe", "version": "1"},
        },
    }


async def _negative_http_checks(
    client: httpx.AsyncClient,
    url: str,
    arguments: dict[str, Any],
) -> dict[str, bool]:
    auth = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
    }
    no_auth = await client.post(url, json=_initialize_payload())
    bad_origin = await client.post(
        url,
        json=_initialize_payload(),
        headers={**auth, "Origin": "https://untrusted.example"},
    )
    bad_protocol = await client.post(
        url,
        json=_initialize_payload("2099-01-01"),
        headers=auth,
    )
    stale_session = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        headers={
            **auth,
            "mcp-session-id": "expired-probe-session",
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        },
    )
    cancellation = await _cancel_http_call(client, url, auth, arguments)
    checks = {
        "no_auth_rejected": no_auth.status_code == 401,
        "origin_rejected": bad_origin.status_code == 403,
        "protocol_rejected": bad_protocol.status_code == 400,
        "stale_session_rejected": stale_session.status_code == 404,
        "cancellation_acknowledged": cancellation,
    }
    if not all(checks.values()):
        raise RuntimeError(f"Streamable HTTP 负向探针失败: {checks}")
    return checks


async def _cancel_http_call(
    client: httpx.AsyncClient,
    url: str,
    auth: dict[str, str],
    arguments: dict[str, Any],
) -> bool:
    initialized = await client.post(url, json=_initialize_payload(), headers=auth)
    session_id = initialized.headers.get("mcp-session-id")
    if initialized.status_code != 200 or not session_id:
        return False
    headers = {
        **auth,
        "mcp-session-id": session_id,
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
    }
    ready = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        headers=headers,
    )
    if ready.status_code != 202:
        return False
    call = asyncio.create_task(
        client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "aggregate_preview",
                    "arguments": arguments,
                    "_meta": _context().to_request_meta(),
                },
            },
            headers=headers,
        )
    )
    await asyncio.sleep(0.1)
    cancelled = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 9, "reason": "stage0 probe"},
        },
        headers=headers,
    )
    response = await asyncio.wait_for(call, timeout=2)
    await client.delete(url, headers=headers)
    body = response.json()
    return (
        cancelled.status_code == 202
        and response.status_code == 200
        and body.get("error", {}).get("message") == "Request cancelled"
    )


async def _probe_http(
    dataset_dir: Path,
    arguments: dict[str, Any],
) -> tuple[TransportResult, dict[str, bool], int | None]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp/"
    env = {
        **os.environ,
        "DATASET_DIR": str(dataset_dir),
        "MCP_TRANSPORT": "streamable-http",
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_PORT": str(port),
        "MCP_SERVICE_TOKEN": TOKEN,
        "MCP_ALLOWED_HOSTS": "127.0.0.1:*",
        "MCP_ALLOWED_ORIGINS": "https://trusted.example",
        "MCP_PROBE_DELAY_SECONDS": "0.5",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.mcp_transport_probe_server",
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_http(url, process)
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            negative = await _negative_http_checks(client, url, arguments)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=3,
            trust_env=False,
        ) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                read_stream, write_stream, get_session_id = streams
                async with ClientSession(read_stream, write_stream) as session:
                    result = await _exercise_session(
                        session,
                        name="streamable_http",
                        arguments=arguments,
                        session_created=get_session_id() is not None,
                    )
                    session_created = get_session_id() is not None
                    if not session_created:
                        raise RuntimeError("Streamable HTTP 未建立 stateful session")
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:
            process.kill()
            await process.wait()
    result = replace(
        result,
        session_created=session_created,
        graceful_close=process.returncode == 0,
    )
    return result, negative, process.returncode


async def run_probe(output: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chatbi-mcp-probe-") as temp:
        dataset_dir = Path(temp) / "datasets"
        dataset_dir.mkdir()
        previous_dataset_dir = os.environ.get("DATASET_DIR")
        os.environ["DATASET_DIR"] = str(dataset_dir)
        get_settings.cache_clear()
        try:
            _, arguments = _write_dataset(dataset_dir)
            direct = aggregate_preview(arguments)
            direct_hash = stable_hash(direct)
            stdio = await _probe_stdio(dataset_dir, arguments)
            http_result, negative, exit_code = await _probe_http(
                dataset_dir, arguments
            )
        finally:
            if previous_dataset_dir is None:
                os.environ.pop("DATASET_DIR", None)
            else:
                os.environ["DATASET_DIR"] = previous_dataset_dir
            get_settings.cache_clear()
    hashes = {direct_hash, stdio.result_hash, http_result.result_hash}
    if len(hashes) != 1:
        raise RuntimeError("aggregate_preview 在直接调用、stdio、HTTP 间输出不等价")
    if {
        stdio.protocol_version,
        http_result.protocol_version,
    } != {MCP_PROTOCOL_VERSION}:
        raise RuntimeError("MCP 协议协商版本不符合固定版本")
    report = {
        "schema": "chatbi-mcp-transport-probe-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "sdk": {"package": "mcp", "version": version("mcp")},
        "protocol_version": MCP_PROTOCOL_VERSION,
        "tool": "aggregate_preview",
        "equivalent": True,
        "direct_result_hash": direct_hash,
        "transports": [stdio.public_dict(), http_result.public_dict()],
        "http_negative_checks": negative,
        "http_process_exit_code": exit_code,
        "raw_data_in_report": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".data/evaluations/v2.4/mcp-transport-probe.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(run_probe(args.output.resolve()))
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "equivalent": report["equivalent"],
                "protocol_version": report["protocol_version"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
