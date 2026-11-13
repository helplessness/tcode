"""Recover an explicitly supplied, task-owned bundle without executing a job."""
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath

from .. import artifact_transport
from ..runtime.shared_tracker import SharedTracker
from .async_protein_run import extract_link
from .manager import AgentWorkspaceManager
from .protein_run import run_root_for


def recover_task_artifacts(*, control, task, bundle_id, service_id=''):
    control = Path(control).resolve()
    link = extract_link(task)
    run = AgentWorkspaceManager.load(control_workdir=str(control), workspace_id=link.get("workspace_id", "")) if link else None
    if (not run or run.session_id != task["session_id"] or run.strategy != "protein_run"
            or Path(run.control_workdir).resolve() != control
            or Path(run.workspace_root).resolve() != run_root_for(control, run.workspace_id)
            or Path(run.outputs_dir).resolve() != Path(run.workspace_root).resolve() / "outputs"
            or not Path(run.outputs_dir).resolve().is_relative_to(control)):
        raise ValueError("任务缺少可验证的持久产物工作区，未下载文件。")
    identifier = task["identifier"]
    workspace = task.get("workspace") or {}
    run_id = str((task.get("job_ref") or {}).get("run_id") or workspace.get("run_id") or "")
    if not run_id:
        raise ValueError("任务缺少运行来源标识，未下载文件。")
    prefix = (workspace["user_id"], workspace["workspace_id"], identifier)

    def validate_paths(files):
        if not isinstance(files, list) or not files:
            raise ValueError("产物包没有可验证的文件清单。")
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("产物包文件清单无效。")
            parts = PurePosixPath(str(item.get("relative_path") or "")).parts
            if parts[:3] != prefix or run_id not in parts[3:] or ".." in parts:
                raise ValueError("产物包的文件归属与当前任务不符，不能关联。")

    selected, service, headers = artifact_transport._artifact_endpoint(service_id)
    metadata = artifact_transport._request_json(f"{service}/api/v1/bundles/{bundle_id}",
                                               **({'headers': headers} if selected else {}))
    validate_paths(metadata.get("files"))
    if metadata.get("bundle_id") != bundle_id or metadata.get("state") != "ready":
        raise ValueError("产物包尚未就绪或引用不匹配。")
    recovered = artifact_transport.materialize_output_bundle(
        {"output_bundle": {"kind": "artifact_bundle", "bundle_id": bundle_id,
                           **({'service_id': selected} if selected else {})}},
        Path(run.outputs_dir) / "recovered_artifacts", preserve_existing=True,
    )
    validate_paths(recovered["files"])
    manifests = [item for item in recovered["files"] if PurePosixPath(item["relative_path"]).name == "run_manifest.json"]
    if len(manifests) != 1 or Path(manifests[0]["path"]).stat().st_size > 1024 * 1024:
        raise ValueError("缺少唯一且有界的运行清单，已下载文件未登记到任务。")
    manifest = json.loads(Path(manifests[0]["path"]).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise ValueError("运行清单与当前任务不符，已下载文件未登记到任务。")
    paths = [str(Path(item["path"]).relative_to(control)) for item in recovered["files"]]
    receipt = {
        "bundle_id": bundle_id, "source": "user_supplied_existing_bundle",
        "service_id": selected,
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "original_status": task["status"], "total_files": recovered["total_files"],
        "total_size": recovered["total_size"], "run_id": run_id,
        "manifest_status": str(manifest.get("status") or "unknown")[:80],
    }
    # Register files only. Do not settle/restart a job or replace its error/result.
    workspace_patch = {"transfer_state": "ready", "output_root": str(Path(recovered["root"]).relative_to(control)),
                       "output_abs_path": recovered["root"], "artifacts": paths, "artifact_recovery": receipt}
    # Recheck after network I/O, under the same lock used by tracker mutations.
    from ..runtime.shared_tracker import _LOCK
    with _LOCK:
        latest = SharedTracker.get_task(task_id=task["id"], workdir=str(control))
        if (not latest or latest["session_id"] != task["session_id"]
                or latest["job_ref"] != task["job_ref"] or latest["status"] != task["status"]):
            raise ValueError("任务已删除或发生变化，已下载文件未登记到任务。")
        updated = SharedTracker.patch_task(
            task_id=task["id"], workdir=str(control), source="user",
            workspace_patch=workspace_patch,
            metadata_patch={"artifact_recovery": receipt}, event_kind="artifacts_recovered",
        )
    if not updated:
        raise ValueError("任务已不存在，已下载文件未登记到任务。")
    return {"success": True, "recovery": receipt, "workspace": workspace_patch}
