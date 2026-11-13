"""零额外依赖的 Streamable HTTP MCP Apps 示例服务。"""

from __future__ import annotations

import argparse
import json
import secrets
from typing import Any

from aiohttp import web


RESOURCE_URI = "ui://tcode-open-source/counter"
MIME_TYPE = "text/html;profile=mcp-app"
SESSIONS: set[str] = set()
RESOURCE_META = {
    "ui": {
        "csp": {
            "connectDomains": ["https://api.example.invalid"],
            "resourceDomains": ["https://cdn.example.invalid"],
        },
        "permissions": {"clipboardWrite": {}},
        "prefersBorder": True,
    }
}


APP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>TCode MCP Apps Counter</title>
  <style>
    :root { color-scheme: dark; font: 14px/1.5 system-ui, sans-serif; }
    body { margin: 0; padding: 20px; color: #dce7ed; background: #11181d; }
    article { border: 1px solid #31404a; border-radius: 14px; padding: 18px; }
    h2 { margin: 0 0 8px; font-size: 18px; }
    p { color: #9fb0ba; }
    button { border: 1px solid #4d9ec7; border-radius: 999px; padding: 8px 14px;
      color: #dff5ff; background: #153342; cursor: pointer; }
    output { display: inline-block; min-width: 2em; color: #86dcff; font-weight: 700; }
    #status { margin-top: 12px; font-size: 12px; }
  </style>
</head>
<body>
  <article data-testid="mcp-app-counter">
    <h2>MCP Apps 互操作计数器</h2>
    <p>当前值：<output id="value">0</output></p>
    <button id="refresh" type="button">通过 Host 增加一次</button>
    <div id="status">正在连接 TCode Host…</div>
  </article>
  <script>
    (() => {
      let nextId = 1;
      const pending = new Map();
      const value = document.getElementById('value');
      const status = document.getElementById('status');
      const send = (message) => window.parent.postMessage(message, '*');
      const request = (method, params) => new Promise((resolve, reject) => {
        const id = nextId++;
        pending.set(id, { resolve, reject });
        send({ jsonrpc: '2.0', id, method, params });
      });
      const notify = (method, params = {}) => send({ jsonrpc: '2.0', method, params });
      const applyResult = (payload) => {
        const count = payload && payload.structuredContent && payload.structuredContent.count;
        if (Number.isFinite(Number(count))) value.textContent = String(count);
      };
      window.addEventListener('message', async (event) => {
        if (event.source !== window.parent) return;
        const message = event.data;
        if (!message || message.jsonrpc !== '2.0') return;
        if (Object.prototype.hasOwnProperty.call(message, 'id') && !message.method) {
          const waiter = pending.get(message.id);
          if (!waiter) return;
          pending.delete(message.id);
          if (message.error) waiter.reject(new Error(String(message.error.message || 'Host error')));
          else waiter.resolve(message.result);
          return;
        }
        if (message.method === 'ui/notifications/tool-result') applyResult(message.params);
        if (message.method === 'ui/notifications/initialized') status.textContent = '已连接 TCode Host';
        if (message.method === 'ui/resource-teardown' && Object.prototype.hasOwnProperty.call(message, 'id')) {
          send({ jsonrpc: '2.0', id: message.id, result: {} });
        }
      });
      document.getElementById('refresh').addEventListener('click', async () => {
        status.textContent = '等待用户确认…';
        try {
          const nextValue = Number(value.textContent || 0) + 1;
          const result = await request('tools/call', {
            name: 'refresh_counter',
            arguments: { step: nextValue }
          });
          applyResult(result);
          status.textContent = 'Host 调用完成';
        } catch (error) {
          status.textContent = String(error && error.message || error);
        }
      });
      // Inline srcdoc scripts finish before the iframe load event. Retry the
      // handshake until the parent has attached its PostMessage transport.
      let initialized = false;
      const initialize = () => {
        request('ui/initialize', {
          appInfo: { name: 'TCode MCP Apps Counter', version: '1.0.0' },
          appCapabilities: {},
          protocolVersion: '2026-01-26'
        }).then(() => {
          if (initialized) return;
          initialized = true;
          notify('ui/notifications/initialized');
          notify('ui/notifications/size-changed', { width: 520, height: 250 });
          status.textContent = '已连接 TCode Host';
        }).catch((error) => { status.textContent = String(error && error.message || error); });
        setTimeout(() => { if (!initialized) initialize(); }, 250);
      };
      initialize();
    })();
  </script>
</body>
</html>"""


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "resource": RESOURCE_URI})


async def mcp(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status=400,
        )
    method = str(payload.get("method") or "")
    request_id = payload.get("id")

    if method == "initialize":
        session_id = secrets.token_urlsafe(18)
        SESSIONS.add(session_id)
        response = web.json_response(_result(request_id, {
            "protocolVersion": str(
                (payload.get("params") or {}).get("protocolVersion") or "2024-11-05"
            ),
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "tcode-mcp-app-example", "version": "1.0.0"},
        }))
        response.headers["mcp-session-id"] = session_id
        return response

    session_id = str(request.headers.get("mcp-session-id") or "")
    if session_id not in SESSIONS:
        return web.json_response(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "Unknown session"}},
            status=404,
        )
    if method == "notifications/initialized":
        return web.Response(status=202)
    if method == "tools/list":
        return web.json_response(_result(request_id, {"tools": [
            {
                "name": "show_counter",
                "description": "显示一个可交互计数器。用户要求测试或显示 MCP App 时调用。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"initial": {"type": "integer", "default": 0}},
                    "additionalProperties": False,
                },
                "_meta": {
                    "ui": {
                        "resourceUri": RESOURCE_URI,
                        "visibility": ["model"],
                    }
                },
            },
            {
                "name": "refresh_counter",
                "description": "增加 MCP App 计数器。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"step": {"type": "integer", "default": 1}},
                    "additionalProperties": False,
                },
                "_meta": {
                    "ui": {
                        "resourceUri": RESOURCE_URI,
                        "visibility": ["app"],
                    }
                },
            },
        ]}))
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == "show_counter":
            count = int(arguments.get("initial") or 0)
        elif name == "refresh_counter":
            count = int(arguments.get("step") or 1)
        else:
            return web.json_response({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            })
        return web.json_response(_result(request_id, {
            "content": [{"type": "text", "text": f"计数器当前值为 {count}"}],
            "structuredContent": {"count": count},
            "isError": False,
        }))
    if method == "resources/list":
        return web.json_response(_result(request_id, {"resources": [{
            "name": "TCode MCP Apps Counter",
            "uri": RESOURCE_URI,
            "mimeType": MIME_TYPE,
            "_meta": RESOURCE_META,
        }]}))
    if method == "resources/read":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if str(params.get("uri") or "") != RESOURCE_URI:
            return web.json_response({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32002, "message": "Resource not found"},
            })
        return web.json_response(_result(request_id, {"contents": [{
            "uri": RESOURCE_URI,
            "mimeType": MIME_TYPE,
            "text": APP_HTML,
            "_meta": RESOURCE_META,
        }]}))
    return web.json_response({
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


def create_app() -> web.Application:
    app = web.Application(client_max_size=1_000_000)
    app.router.add_get("/health", health)
    app.router.add_post("/mcp", mcp)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10131)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port, print=None)
    return 0
