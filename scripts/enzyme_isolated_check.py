"""新建隔离目录，以当前配置的模型执行一次 Workflow；不读取旧会话或 gold。

--prepare-only 仅准备环境。无该参数时调用真实 CLI，保留日志，不自动重跑。
目录及配置为 0700/0600；Agent 无配置/源码/历史/任意网络读取权限。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/enzyme_reaction_discovery"
TEMPLATE_KEY = "enzyme-reaction-discovery.mechanism-guided-enzyme-search"
STAGE_IDS = ("reaction_analysis", "mechanism_and_capability", "family_screening_and_routes", "critical_report")


def summarize(workdir):
    """Inspect durable results, never equate a successful CLI exit with a pass.

    This is an engineering check only; R1 and scientific acceptance are separate.
    No confirmation is submitted and no run is resumed by this function.
    """
    root = Path(workdir).resolve(strict=True)
    runs = [json.loads(path.read_text()) for path in sorted((root / ".tcode/workflow/runs").glob("*.json"))]
    runs = [run for run in runs if run.get("template_key") == TEMPLATE_KEY]
    if not runs:
        # Startup confirmation legitimately has no Run yet. Report the pending
        # gate instead of misclassifying it as a missing/corrupt workflow file.
        attempts = []
        for path in (root / ".tcode/debug/tool_logs/sessions").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("tool_name") == "workflow_run":
                    attempts.append(item)
        if attempts:
            latest = max(attempts, key=lambda item: item.get("timestamp", ""))
            metadata = (latest.get("result") or {}).get("metadata") or {}
            if metadata.get("status") == "awaiting_confirmation":
                return {"workflow_completed": False, "reason": "awaiting_start_confirmation",
                        "status": "awaiting_confirmation", "run_count": 0,
                        "r1_acceptance": "not_started", "scientific_pass": "not_evaluated"}
    if len(runs) != 1:
        return {"workflow_completed": False, "reason": "expected_one_isolated_workflow", "run_count": len(runs)}
    run = runs[0]
    stages = run.get("stage_states") or []
    missing = []
    specs = {s["stage_id"]: s for s in (run.get("metadata", {}).get("template_snapshot", {}).get("stages") or [])}
    for stage in stages:
        refs = stage.get("artifact_refs") or {}
        expected = specs.get(stage.get("stage_id"), {}).get("expected_artifacts") or []
        for name in expected:
            ref = refs.get(name)
            path = (root / ref).resolve() if isinstance(ref, str) and ref else None
            if path is None or not path.is_relative_to(root) or not path.is_file():
                missing.append(name)
    final = next((s for s in stages if s.get("stage_id") == "critical_report"), {})
    for name in ("07_final_report.json", "07_final_report.md", "mechanism_routes.mmd", "06_critique.json"):
        ref = (final.get("artifact_refs") or {}).get(name)
        path = (root / ref).resolve() if isinstance(ref, str) and ref else None
        if path is None or not path.is_relative_to(root) or not path.is_file():
            missing.append(name)
    complete = (run.get("status") == "completed"
                and [s.get("stage_id") for s in stages] == list(STAGE_IDS)
                and all(s.get("status") == "committed" for s in stages) and not missing)
    return {"workflow_completed": complete, "run_id": run.get("run_id"), "status": run.get("status"),
            "stages": [{k: s.get(k) for k in ("stage_id", "status", "attempt", "last_error")} for s in stages],
            "missing_artifacts": sorted(set(missing)), "r1_acceptance": "not_asserted", "scientific_pass": "not_asserted"}


def write_private(path, data):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def prepare(source, prompt):
    source = Path(source).resolve(strict=True)
    raw = json.loads((source / "tcode.json").read_text())
    workdir = Path(tempfile.mkdtemp(prefix="tcode-enzyme-reliability-" )).resolve()
    cfg = {key: copy.deepcopy(raw[key]) for key in ("model", "provider", "embeddings", "runtime") if key in raw}
    if not cfg.get("model"):
        raise ValueError("当前配置未指定模型；不自动换模型")
    cfg["learning"] = {"enabled": False}
    cfg["agent"] = {"build": {"permission": {
        "*": "deny", "workflow_run": "allow", "workflow_status": "allow",
        "workflow_list_templates": "allow", "question": "allow",
        # The host's tool-visibility filter does not enable a denied tool from
        # nested argument rules. This fresh workspace registers only this plugin.
        "skill": "allow",
    }}}
    write_private(workdir / "tcode.json", cfg)
    (workdir / "plugins").mkdir()
    (workdir / "plugins/enzyme_reaction_discovery").symlink_to(PLUGIN, target_is_directory=True)
    write_private(workdir / "plugins.json", {"auto_discover": False, "plugins": [{
        "name": "enzyme-reaction-discovery", "path": "./plugins/enzyme_reaction_discovery", "enabled": True,
        "config": {"offline_mode": True, "workspace_dir": "./.tcode/plugins/enzyme_reaction_discovery",
                   "max_route_steps": 3},
    }]})
    write_private(workdir / "mcp.json", {})
    write_private(workdir / "mcp_agent_config.json", {})
    write_private(workdir / "check_input.json", {"prompt": prompt, "model": cfg["model"],
        "knowledge_shared_only": True, "r1_unchanged": True, "frontend_test": False})
    return workdir, isolated_environment(source, workdir, cfg)


def isolated_environment(source, workdir, cfg=None):
    """Restore the same isolation for an explicitly approved continuation."""
    source = Path(source).resolve(strict=True)
    workdir = Path(workdir).resolve(strict=True)
    if cfg is None:
        cfg = json.loads((workdir / "tcode.json").read_text())
    env = dict(os.environ)
    # Only host-side model credential references are copied from local.env.
    wanted = {value for group in (cfg.get("provider", {}).values(), [cfg.get("embeddings", {})])
              for item in group for key, value in item.get("options", {}).items()
              if key.endswith("Env") and isinstance(value, str)}
    env_path = source / "local.env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() in wanted:
                env.setdefault(key.strip(), value.strip().strip("\"'"))
    for name in tuple(env):
        if name.startswith(("TCODE_", "OPENCODE_")) and name not in wanted:
            env.pop(name)
    env.update(TCODE_WORKDIR=str(workdir), TCODE_CONFIG_ROOT=str(workdir), TCODE_ENV_FILE=os.devnull,
               TCODE_MCP_CONFIG=str(workdir / "mcp.json"), TCODE_LEARNING_ENABLED="false",
               TCODE_REFLECTION_ENABLED="false", TCODE_COMPACTION_FLUSH_ENABLED="false",
               TCODE_SKILL_DRAFT_ENABLED="false", TCODE_RESEARCH_ENABLED="false",
               TCODE_GLOBAL_MEMORY_WRITE_MODE="none", PYTHONPATH=str(ROOT),
               TCODE_OUTCOME_GUARD="1", TCODE_OUTCOME_SEMANTIC_VALIDATION="always",
               TCODE_PROOF_PACK_ENABLED="1", TCODE_EXPOSE_OUTCOME_VALIDATION="1",
               TCODE_DETERMINISTIC_VERIFIER_ENABLED="1",
               TCODE_OUTCOME_VALIDATOR_TIMEOUT_SECONDS="90",
               TCODE_OUTCOME_VALIDATOR_MAX_TOKENS="2048")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root")
    parser.add_argument("--prompt")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--check-workdir", help=argparse.SUPPRESS)
    parser.add_argument("--summarize-workdir", help="只读检查已有隔离运行，不调用模型、不重跑")
    args = parser.parse_args()
    if args.summarize_workdir:
        summary = summarize(args.summarize_workdir)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0 if summary["workflow_completed"] else 2
    if args.check_workdir:
        from tcode_agent.plugins.manager import PluginManager
        async def preflight():
            manager = await PluginManager.get_global(args.check_workdir)
            errors = manager.get_load_errors()
            if errors:
                raise ValueError("插件预检失败：" + json.dumps(errors, ensure_ascii=False))
            plugins = manager.list_available_plugins()
            if not any(p.get("name") == "enzyme-reaction-discovery" for p in plugins):
                raise ValueError("插件预检失败：未发现酶插件")
            from tcode_agent.workflow.registry import WorkflowTemplateRegistry
            template = WorkflowTemplateRegistry.get("enzyme-reaction-discovery.mechanism-guided-enzyme-search")
            if template is None or len(template.stages) != 4:
                raise ValueError("插件预检失败：四阶段模板未注册")
            print("插件配置预检通过；未调用推理模型。", flush=True)
        asyncio.run(preflight())
        return 0
    if not args.config_root or not args.prompt:
        parser.error("需要 --config-root 和 --prompt")
    workdir, env = prepare(args.config_root, args.prompt)
    print(json.dumps({"workdir": str(workdir), "frontend_test": False, "automatic_rerun": False}), flush=True)
    if args.prepare_only:
        return 0
    check = subprocess.run([sys.executable, "-m", "scripts.enzyme_isolated_check", "--check-workdir", str(workdir)],
                           cwd=workdir, env=env)
    if check.returncode:
        return check.returncode
    command = [sys.executable, str(ROOT / "scripts/chat_cli.py"), "--workdir", str(workdir),
               "--agent", "build", "--once", "--prompt", args.prompt]
    with (workdir / "cli.log").open("x", encoding="utf-8") as log:
        os.chmod(workdir / "cli.log", 0o600)
        result = subprocess.run(command, cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT)
    summary = summarize(workdir)
    print(json.dumps({"exit_code": result.returncode, "log": str(workdir / "cli.log"), **summary}, ensure_ascii=False), flush=True)
    return result.returncode or (0 if summary["workflow_completed"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
