"""Persistent remote-job example. Run on the third-party server, not in plugin.py."""
import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastmcp import FastMCP
from artifact_io import ArtifactIO

token = os.environ.get('EXAMPLE_MCP_TOKEN', '')
auth = None
if token:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    auth = StaticTokenVerifier(tokens={token: {'client_id': 'example', 'scopes': []}})
mcp = FastMCP('Third-party compute example', auth=auth)
root = Path(os.environ.get('EXAMPLE_DATA_ROOT', './example-compute-data')).resolve()


@contextmanager
def database():
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'jobs.sqlite', timeout=30)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, arguments TEXT, status TEXT, ready_at REAL, result TEXT)')
        with db:
            yield db
    finally:
        db.close()


@mcp.tool
def lookup(text: str) -> dict:
    """Short direct-call tool. This is not an asynchronous computation."""
    return {'text': text, 'source': 'community-lookup'}


@mcp.tool
def submit_prediction(request_id: str, tool: str, arguments: dict) -> dict:
    """Idempotent remote-job v1 submit. Repeated request_id never starts a second computation."""
    if tool != 'predict' or not (arguments.get('input_file') or arguments.get('input_directory')):
        raise ValueError('predict requires input_file or input_directory')
    identifier = 'remote-' + hashlib.sha256(request_id.encode()).hexdigest()[:32]
    encoded = json.dumps(arguments, sort_keys=True)
    delay = max(0, min(int(arguments.get('delay_seconds', 2)), 300))
    with database() as db:
        existing = db.execute('SELECT arguments FROM jobs WHERE id=?', (identifier,)).fetchone()
        if existing and existing[0] != encoded:
            raise ValueError('request_id was reused for different arguments')
        db.execute('INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, ?, ?)', (identifier, encoded, 'running', time.time() + delay, ''))
    return {'job_id': identifier, 'status': 'running'}


def record(identifier):
    with database() as db:
        row = db.execute('SELECT arguments,status,ready_at,result FROM jobs WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise ValueError('job not found')
    return row


@mcp.tool
def get_prediction_status(job_id: str) -> dict:
    row = record(job_id)
    status = row[1]
    if status == 'running' and time.time() >= row[2]:
        status = 'completed'
    return {'job_id': job_id, 'status': status}


@mcp.tool
def cancel_prediction(job_id: str) -> dict:
    record(job_id)
    with database() as db:
        db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
    return {'job_id': job_id, 'status': 'cancelled'}


@mcp.tool
def get_prediction_result(job_id: str) -> dict:
    args, status, ready_at, saved = record(job_id)
    if status == 'cancelled' or time.time() < ready_at:
        raise ValueError('job is not completed')
    if saved:
        return json.loads(saved)
    arguments = json.loads(args)
    # Directory name is a hash-derived remote job ID, never a caller path.
    task = root / job_id
    if task.exists():
        shutil.rmtree(task)
    output = task / 'output'
    output.mkdir(parents=True)
    io = ArtifactIO()
    if arguments.get('input_file'):
        io.materialize(arguments['input_file'], output / 'inputs/input.dat')
    if arguments.get('input_directory'):
        io.materialize(arguments['input_directory'], output / 'inputs/directory')
    report = output / 'reports/summary.json'
    report.parent.mkdir()
    report.write_text(json.dumps({'job_id': job_id, 'message': '第三方计算完成', 'files': [p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()]}, ensure_ascii=False), encoding='utf-8')
    result = {'status': 'completed', 'summary': '示例计算完成，已上传输入副本和报告', 'output_bundle': io.upload_directory(output)}
    with database() as db:
        db.execute("UPDATE jobs SET status='completed',result=? WHERE id=?", (json.dumps(result), job_id))
    return result


if __name__ == '__main__':
    mcp.run(transport='streamable-http', host=os.environ.get('EXAMPLE_HOST', '127.0.0.1'), port=int(os.environ.get('EXAMPLE_PORT', '10230')))
