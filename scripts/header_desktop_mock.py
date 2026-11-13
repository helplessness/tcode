"""Loopback-only read-only MCP fixture for manual desktop acceptance.

Run through header-desktop-acceptance.cjs. Logs only fictional test context.
No business services, files, Artifact or jobs are accessed.
"""
import asyncio
import json
import sys
from uuid import uuid4

from aiohttp import web

sessions, events = {}, []
active = 0
peak = 0
overlap = asyncio.Event()
SCHEMA = {'type': 'object', 'properties': {
    'query': {'type': 'string', 'description': '查询内容，例如全部结果汇总'},
    'request_label': {'type': 'string', 'description': '用户指定的回显标签，原样返回用于核对请求'}},
    'required': ['query', 'request_label'], 'additionalProperties': False}


def record(event):
    events.append(event)
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def mcp(request):
    global active, peak
    project = request.headers.get('X-Project-ID')
    session = request.headers.get('Mcp-Session-Id')
    if request.method == 'DELETE':
        record({'phase': 'delete', 'project': project, 'session': session})
        return web.Response(status=200)
    if request.method != 'POST':
        return web.Response(status=405)
    message = await request.json()
    method = message['method']
    if method == 'notifications/initialized':
        return web.Response(status=202)
    if method == 'initialize':
        session = uuid4().hex
        sessions[session] = project
        record({'phase': method, 'project': project, 'session': session})
        return web.json_response({'jsonrpc': '2.0', 'id': message['id'], 'result': {
            'protocolVersion': message['params']['protocolVersion'], 'capabilities': {},
            'serverInfo': {'name': 'local-header-echo', 'version': '1'}}},
            headers={'Mcp-Session-Id': session})
    if session not in sessions or sessions[session] != project:
        return web.Response(status=409, text='Session header mismatch')
    if method == 'tools/list':
        result = {'tools': [{'name': 'query', 'description': '只读模拟查询，返回收到的项目上下文和标签；不访问真实数据。',
                             'inputSchema': SCHEMA, 'annotations': {'readOnlyHint': True, 'destructiveHint': False}}]}
    elif method == 'tools/call':
        args = message['params']['arguments']
        active += 1
        peak = max(peak, active)
        record({'phase': 'call_start', 'project': project, 'session': session, 'arguments': args, 'active': active})
        try:
            if args.get('request_label') in {'parallel-a', 'parallel-b'}:
                if active >= 2:
                    overlap.set()
                await asyncio.wait_for(overlap.wait(), 90)
            payload = {'project': project, 'request_label': args.get('request_label'),
                       'query': args.get('query'), 'session': session, 'max_concurrent': peak,
                       'host_context_leaked': '_tcode_context' in args}
            result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                      'structuredContent': payload, 'isError': False}
            record({'phase': 'call_end', **payload})
        except TimeoutError:
            record({'phase': 'overlap_timeout', 'project': project})
            result = {'content': [{'type': 'text', 'text': 'Second parallel call did not arrive within 90 seconds'}], 'isError': True}
        finally:
            active -= 1
    else:
        result = {}
    return web.json_response({'jsonrpc': '2.0', 'id': message['id'], 'result': result})


async def main():
    app = web.Application()
    app.router.add_route('*', '/mcp', mcp)
    async def evidence(request):
        return web.json_response({'events': events, 'active': active, 'max_concurrent': peak})
    app.router.add_get('/evidence', evidence)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    print(json.dumps({'ready': True, 'port': site._server.sockets[0].getsockname()[1]}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
