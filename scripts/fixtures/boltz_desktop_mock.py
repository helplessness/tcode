"""Loopback-only fixture; never imports or executes Boltz."""
import hashlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.dshlib.smoke import SmokeHTTPServer, SmokeRequestHandler, StubState

FILES = [{"pointer": "/tools_input", "kind": "file", "transport": "artifact"}]
JOBS = {}
ROOT = Path(sys.argv[1])
TOOLS = ("submit_job", "describe_target_tool", "get_job_status", "collect_job_result")
PDB = ("HEADER    SYNTHETIC FIXTURE\n" + "".join(
    f"ATOM  {i:5d}  CA  ALA A{i:4d}    {i*3.8:8.3f}{(i%2)*1.2:8.3f}{0:8.3f}  1.00 20.00           C\n"
    for i in range(1, 7)
) + "END\n").encode()


class Handler(SmokeRequestHandler):
    def do_GET(self):
        if self.path == "/api/v1/bundles/mock-structure":
            return self._send_json({"bundle_id": "mock-structure", "state": "ready", "files": [{
                "artifact_id": "mock-pdb", "relative_path": "mock-structure.pdb",
                "size": len(PDB), "sha256": hashlib.sha256(PDB).hexdigest(),
            }]})
        if self.path == "/api/v1/artifacts/mock-pdb/content":
            self.send_response(200)
            self.send_header("Content-Length", str(len(PDB)))
            self.end_headers()
            self.wfile.write(PDB)
            print("mock PDB served", flush=True)
            return
        self.send_error(404)

    def _handle_openai(self, payload):
        if not payload.get("stream"):
            return super()._handle_openai(payload)
        last = next((m.get("content") for m in reversed(payload["messages"]) if m["role"] == "user"), "")
        mark = str(last)
        tools = [t["function"]["name"] for t in payload.get("tools") or []]
        name = next((n for n in tools if "submit_job" in n), "")
        delta = {"role": "assistant", "content": "本机模拟回复。"}
        finish = "stop"
        if "FIXTURE_BOLTZ" in mark and name and payload["messages"][-1]["role"] == "user":
            meta = json.loads(mark.split("FIXTURE_BOLTZ ", 1)[1].splitlines()[0])
            args = {"target_server": "boltz-mcp", "target_tool": "boltz_mcp_server", **meta}
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_fixture",
                     "type": "function", "function": {"name": name, "arguments": json.dumps({"input_config": args})}}]}
            finish = "tool_calls"
        data = "".join("data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk",
            "model": "smoke-model", "choices": [{"index": 0, "delta": d, "finish_reason": f}]}) + "\n\n"
            for d, f in [(delta, None), ({}, finish)]) + "data: [DONE]\n\n"
        body = data.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_mcp(self, payload):
        method = payload.get("method")
        headers = {"mcp-session-id": "mock-boltz"}
        if method in ("initialize", "notifications/initialized"):
            return super()._handle_mcp(payload)
        if method == "tools/list":
            value = {"tools": [{"name": name, "description": name, "inputSchema": {
                "type": "object", "properties": {"input": {"type": "object"}, "input_config": {"type": "object"}}
            }} for name in TOOLS]}
        else:
            params = payload.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            data = args.get("input") or args.get("input_config") or args
            if name == "describe_target_tool":
                out = {"ok": True, "target_server": "boltz-mcp", "target_tool": "boltz_mcp_server",
                       "validation": {"valid": True}, "files": FILES,
                       "files_hash": hashlib.sha256(json.dumps({"files": FILES}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]}
            elif name == "submit_job" and data.get("target_server") == "boltz-mcp":
                job = str(uuid.uuid4())
                JOBS[job] = len(JOBS) + 1
                out = {"job_id": job, "process_id": job}
                print("mock accepted", JOBS[job], flush=True)
            elif data.get("job_id") in JOBS and name in ("get_job_status", "collect_job_result"):
                job = data["job_id"]
                index = JOBS[job]
                out = {"job_id": job, "process_id": job,
                       "status": "completed" if (ROOT / f"release-{index}").exists() else "running",
                       "target_result": {"fixture_round": index, "marker": f"mock-output-{index}"}}
                if name == "collect_job_result":
                    out["output_bundle"] = {"kind": "artifact_bundle", "bundle_id": "mock-structure"}
            else:
                out = {"ok": False, "error": "not a fixture job"}
            value = {"structuredContent": out, "content": [{"type": "text", "text": json.dumps(out)}], "isError": False}
        self._send_json({"jsonrpc": "2.0", "id": payload.get("id"), "result": value}, headers=headers)


server = SmokeHTTPServer(("127.0.0.1", 0), StubState())
server.RequestHandlerClass = Handler
print(server.server_port, flush=True)
server.serve_forever()
