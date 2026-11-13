"""Unprivileged MCP/CLI forwarder for the TCode in-process platform bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from .transports.stdio_mcp import forward_bridge_request


def _environment() -> tuple[str, str]:
    return (
        str(os.environ.get("TCODE_PLATFORM_BRIDGE_SOCKET") or "").strip(),
        str(os.environ.get("TCODE_PLATFORM_BRIDGE_TOKEN") or "").strip(),
    )


async def _forward(request: dict[str, Any]) -> dict[str, Any]:
    socket_path, token = _environment()
    return await forward_bridge_request(socket_path, token, request)


def _mcp_result(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("ok") is not True:
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        text = str(error.get("message") or "TCode platform bridge call failed")
        return {"content": [{"type": "text", "text": text}], "isError": True}
    result = response.get("result")
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": result if isinstance(result, dict) else {"value": result},
        "isError": False,
    }


async def serve_stdio() -> int:
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            return 0
        request_id: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("Expected a JSON-RPC 2.0 object")
            request_id = request.get("id")
            method = str(request.get("method") or "")
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "tcode-platform", "version": "1.0.0"},
                }
            elif method == "tools/list":
                response = await _forward({"action": "list"})
                if response.get("ok") is not True:
                    raise RuntimeError(str((response.get("error") or {}).get("message") or "Bridge authorization failed"))
                result = {"tools": list(response.get("tools") or [])}
            elif method == "tools/call":
                params = request.get("params") if isinstance(request.get("params"), dict) else {}
                response = await _forward(
                    {
                        "action": "call",
                        "name": str(params.get("name") or ""),
                        "arguments": params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
                    }
                )
                result = _mcp_result(response)
            elif method.startswith("notifications/"):
                continue
            else:
                raise ValueError(f"Unsupported MCP method: {method}")
            response_payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            response_payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32001, "message": str(exc)[:2000]},
            }
        sys.stdout.write(json.dumps(response_payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


async def call_cli(tool_name: str, arguments: str) -> int:
    try:
        parsed = json.loads(arguments or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("arguments must be a JSON object")
        response = await _forward({"action": "call", "name": tool_name, "arguments": parsed})
        sys.stdout.write(json.dumps(response, ensure_ascii=False, indent=2) + "\n")
        return 0 if response.get("ok") is True else 1
    except Exception as exc:
        sys.stderr.write(str(exc)[:2000] + "\n")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TCode platform bridge MCP forwarder")
    subparsers = parser.add_subparsers(dest="command")
    call = subparsers.add_parser("call")
    call.add_argument("tool_name")
    call.add_argument("--arguments", default="{}")
    args = parser.parse_args(argv)
    if args.command == "call":
        return asyncio.run(call_cli(args.tool_name, args.arguments))
    return asyncio.run(serve_stdio())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "serve_stdio"]
