"""Research annotations for Boltz Protein Runs; never dispatches work."""
from pathlib import Path

BOLTZ = ("boltz-mcp", "boltz_mcp_server")


def owner_for(context):
    from ..workspace.async_protein_run import _context_control_workdir
    extra = context.extra or {}
    owner = {key: str(extra.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id")}
    if not all(owner.values()):
        raise ValueError("实验计算需要明确的工作区归属。")
    return {**owner, "workdir": _context_control_workdir(context)}


def experiment_for(context, experiment_id):
    from .experiment_store import ResearchExperimentStore
    item = ResearchExperimentStore.get_experiment(experiment_id, **owner_for(context))
    if item.frozen_at:
        raise ValueError("实验已经冻结，请新建后续实验。")
    return item


def next_round(context, experiment_id, source_id, replacement):
    from fastapi import HTTPException
    from ..server.protein_runs_api import _runs_root, _safe_run_dir, _active_attempt, _attempt_records, _attempt_arguments
    from ..workspace.async_protein_run import remap_submission_arguments
    from ..workspace.async_protein_run import resolve_context_input_path
    from ..workspace import AgentWorkspaceManager
    from ..workspace.protein_run import validate_run_inputs

    control = Path(owner_for(context)["workdir"]).resolve()
    try:
        _, manifest = _safe_run_dir(source_id, runs_root=_runs_root(control), workspace_path=control)
    except HTTPException as exc:
        raise ValueError("上一轮不在当前工作区或已不可用。") from exc
    if manifest.get("contract", {}).get("research_experiment_id") != experiment_id:
        raise ValueError("上一轮不属于所选实验。")
    attempt = _active_attempt(_attempt_records(manifest, {}))
    if (attempt.get("target_server"), attempt.get("target_tool")) != BOLTZ or attempt.get("status") != "completed":
        raise ValueError("请选择已完成的 Boltz 运行。")
    workspace = AgentWorkspaceManager.load(control_workdir=control, workspace_id=source_id)
    if not workspace or validate_run_inputs(workspace):
        raise ValueError("上一轮输入已经变化或不可用。请重新选择输入。")
    arguments = _attempt_arguments(manifest, attempt)
    if not arguments or not (arguments.get("input_config", arguments) or {}).get("tools_input"):
        raise ValueError("上一轮未保存可复用的 Boltz 输入。")
    arguments = remap_submission_arguments(
        {"workspace_id": source_id, "run_id": source_id, "control_workdir": str(control),
         "run_root": workspace.workspace_root}, arguments,
    )
    replacement_path = resolve_context_input_path(context, replacement)
    if replacement_path is None or not replacement_path.is_file():
        raise ValueError("请上传或选择一个可用的新主输入文件。")
    inputs = arguments.get("input_config", arguments)
    if Path(inputs["tools_input"]).resolve() == replacement_path:
        raise ValueError("新主输入与上一轮是同一个文件。")
    inputs["tools_input"] = replacement
    return arguments


def rounds_for(workdir, experiment_ids):
    from ..server.protein_runs_api import _runs_root, _safe_run_dir, _summary, _output_view
    from fastapi import HTTPException

    control = Path(workdir).resolve()
    root = _runs_root(control)
    rows = {key: [] for key in experiment_ids}
    if not rows or not root.is_dir():
        return rows
    for path in root.iterdir():
        try:
            _, manifest = _safe_run_dir(path.name, runs_root=root, workspace_path=control)
        except HTTPException:
            continue
        contract = manifest.get("contract") or {}
        key = contract.get("research_experiment_id") if isinstance(contract, dict) else None
        if not isinstance(key, str) or key not in rows:
            continue
        summary = _summary(manifest, run_id=path.name, workspace_path=control)
        rows[key].append({
            **{field: summary[field] for field in ("run_id", "lifecycle_status", "created_at")},
            "source_run_id": contract.get("source_protein_run_id", ""),
            "outputs": [_output_view(item, run_id=path.name) for item in manifest.get("outputs", [])
                        if isinstance(item, dict)],
        })
    for items in rows.values():
        items.sort(key=lambda item: (item["created_at"], item["run_id"]), reverse=True)
    return rows
