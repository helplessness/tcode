"""Query declared, materialized async artifacts without rerunning a job."""
import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field
from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...runtime.shared_tracker import SharedTracker
from ...session.manager import SessionManager
from ...runtime.sandbox.macos_native import publish_file, MAX_BYTES
from ...workspace.manager import AgentWorkspaceManager
from ...workspace.async_protein_run import extract_link
from ... import artifact_transport


async def _query_remote_catalog(parsed, context, control, catalog):
    """Select only from the task's persisted manifest, never a user-supplied URL."""
    bundle_id = str(catalog.get("bundle_id") or "")
    service_id = catalog.get("service_id", "")
    # Resolve only trusted configured services, preserving the bundle namespace.
    selected_service, _, _ = artifact_transport._artifact_endpoint(service_id)
    entries = catalog.get("files")
    if (not artifact_transport._PORTABLE_ID.fullmatch(bundle_id) or not isinstance(entries, list)
            or len(entries) > artifact_transport._env_int("TCODE_ARTIFACT_MATERIALIZE_MAX_FILES",
                artifact_transport._env_int("TMCP_ARTIFACT_MAX_BUNDLE_FILES", 10_000))):
        raise ValueError("artifact_manifest_unavailable")
    files = {}
    paths = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("artifact_manifest_unavailable")
        relative = artifact_transport._safe_relative_path(item.get("relative_path"), max_depth=32).as_posix()
        if relative in paths:
            raise ValueError("artifact_manifest_unavailable")
        paths.add(relative)
        reference = hashlib.sha256((parsed.identifier + ":" + str(selected_service) + ":" + bundle_id + ":" + relative).encode()).hexdigest()
        files[reference] = item
    if parsed.operation == "list":
        extension = parsed.extension.lower().lstrip(".")
        rows = [{"reference": ref, "name": Path(item["relative_path"]).name,
                 "relative_path": item["relative_path"], "size": item["size"],
                 "sha256": item["sha256"], "download_policy": "on_demand"}
                for ref, item in sorted(files.items(), key=lambda pair: pair[1]["relative_path"])
                if not extension or item["relative_path"].lower().endswith("." + extension)]
        end = parsed.offset + parsed.limit
        return {"items": rows[parsed.offset:end], "total": len(rows), "complete": True,
                "next_offset": end if end < len(rows) else None, "source": "remote_manifest",
                "note": "清单已就绪，文件按需下载；清单不检查既有本地副本，resolve仅取所选文件，不提交计算。"}
    item = files.get(parsed.reference)
    if item is None:
        raise ValueError("artifact_reference_unavailable")
    extra = context.extra or {}
    execution = Path(str(extra.get("workdir") or control)).resolve()
    destination = "outputs/artifact_query/" + parsed.reference
    bundle_directory = f"{selected_service}--{bundle_id}" if selected_service else bundle_id
    relative_target = destination + "/" + bundle_directory + "/" + item["relative_path"]
    visible = "/workspace/" + relative_target
    from ..path_policy import resolve_workspace_io_path
    decision = resolve_workspace_io_path(visible, context, tool_id="read", operation="read")
    if decision.blocked or Path(decision.host_path).resolve() != execution / relative_target:
        raise ValueError("artifact_read_not_allowed")
    if not callable(extra.get("__permission_ask__")):
        raise ValueError("artifact_read_authorization_unavailable")
    if not await context.ask("read", [visible]):
        raise ValueError("artifact_read_denied")
    saved = await asyncio.to_thread(
        artifact_transport.materialize_output_bundle,
        {"output_bundle": {"bundle_id": bundle_id, "service_id": selected_service}}, execution / destination,
        selected_paths=[item["relative_path"]], expected_file=item,
        preserve_existing=True, rename_conflicts=False,
    )
    if not saved or len(saved["files"]) != 1:
        raise ValueError("artifact_reference_unavailable")
    return {"reference": parsed.reference, "path": visible, "size": item["size"], "sha256": item["sha256"]}


def _read_declared_file(root, relative):
    """Read a bounded regular file through no-follow directory descriptors."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                raise ValueError("artifact_file_unsupported")
            data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("artifact_file_too_large")
        return data
    finally:
        os.close(fd)


def _import_declared_file(control, relative, execution, destination):
    try:
        data = _read_declared_file(control, relative)
    except FileNotFoundError as exc:
        raise ValueError("artifact_file_missing") from exc
    try:
        # Create-only publication: never take the shared helper's replace branch.
        publish_file(execution, destination, data, expected=None)
    except (FileExistsError, ValueError):
        # An identical previous import is reusable, but is never rewritten.
        try:
            existing = _read_declared_file(execution, destination)
        except (OSError, ValueError) as exc:
            raise ValueError("artifact_target_conflict") from exc
        if existing != data:
            raise ValueError("artifact_target_conflict")
    except FileNotFoundError as exc:
        raise ValueError("artifact_target_unavailable") from exc


class ArtifactQueryParameters(ParameterSchema):
    identifier: str
    operation: Literal["list", "resolve"] = "list"
    extension: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    reference: str = ""


class ArtifactQueryTool(Tool):
    @property
    def id(self):
        return "artifact_query"

    @property
    def description(self):
        return ("List local or remote files declared by an async task in this session, using its LOCAL identifier. "
                "Supports extension filtering and offset/limit pagination; follow next_offset until null. "
                "Resolve a returned reference to a workspace path for read/inspect_file or MCP input. "
                "Resolution copies only that declared file into isolation, never overwrites changed files, "
                "and may download only the selected remote file after read authorization and checksum validation. "
                "Large bundles are manifest-only: inspect size and runner output conventions, then resolve required files. "
                "Never submits or polls computations or changes original inputs. "
                "Unavailable or incomplete artifacts are not successful scientific results.")

    @property
    def parameters(self):
        return ArtifactQueryParameters

    @property
    def permission_dependencies(self):
        return ("read",)

    async def execute(self, args, context: ToolContext):
        try:
            parsed = ArtifactQueryParameters(**args)
            extra = context.extra or {}
            owner = {key: str(extra.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id")}
            if not all(owner.values()):
                raise ValueError("artifact_owner_required")
            session = await SessionManager.get(context.session_id, **owner)
            if session is None:
                raise ValueError("artifact_source_unavailable")
            control = Path(session.workdir).resolve()
            task = await asyncio.to_thread(SharedTracker.get_task, identifier=parsed.identifier, workdir=str(control))
            workspace = (task or {}).get("workspace") or {}
            if (not task or task.get("session_id") != context.session_id
                    or any(workspace.get(key) != value for key, value in owner.items())):
                raise ValueError("artifact_source_unavailable")
            if workspace.get("transfer_state") != "ready":
                raise ValueError("artifact_transfer_not_ready")
            catalog = workspace.get("artifact_catalog")
            if isinstance(catalog, dict):
                result = await _query_remote_catalog(parsed, context, control, catalog)
                return ToolResult(title="任务产物", output=json.dumps(result, ensure_ascii=False))
            if not workspace.get("output_root") or not isinstance(workspace.get("artifacts"), list):
                raise ValueError("artifact_manifest_unavailable")
            raw_root = Path(str(workspace.get("output_root") or ""))
            root = raw_root if raw_root.is_absolute() else control / raw_root
            if root == control or not root.is_relative_to(control) or ".." in root.parts:
                raise ValueError("artifact_root_invalid")
            declared = workspace.get("artifacts") or []
            manifest_source = "materialized_tracker_manifest"
            if not declared:
                # Older Protein Runs retained the full download list in their
                # registered checkpoint rather than the tracker projection.
                link = extract_link(task)
                run_id = str(link.get("workspace_id") or "")
                if (not run_id or Path(run_id).name != run_id
                        or str(link.get("control_workdir") or "") != str(control)):
                    raise ValueError("artifact_manifest_unavailable")
                run = await asyncio.to_thread(AgentWorkspaceManager.load, control_workdir=str(control), workspace_id=run_id)
                if (run is None or run.session_id != context.session_id
                        or Path(run.control_workdir).resolve() != control):
                    raise ValueError("artifact_manifest_unavailable")
                jobs = run.checkpoint_state.get("external_jobs") or []
                matches = [job for job in jobs if job.get("identifier") == parsed.identifier]
                if len(matches) != 1:
                    raise ValueError("artifact_manifest_unavailable")
                metadata = matches[0].get("metadata") or {}
                materialized = metadata.get("artifact_materialization") or {}
                if isinstance(materialized.get("remote_catalog"), dict):
                    result = await _query_remote_catalog(parsed, context, control, materialized["remote_catalog"])
                    return ToolResult(title="任务产物", output=json.dumps(result, ensure_ascii=False))
                downloaded = metadata.get("downloaded_artifacts")
                if (materialized.get("status") != "succeeded" or not isinstance(downloaded, list)
                        or materialized.get("total_files") != len(downloaded)):
                    raise ValueError("artifact_manifest_unavailable")
                run_root = Path(run.workspace_root)
                if not run_root.is_relative_to(control) or run_root == control:
                    raise ValueError("artifact_manifest_unavailable")
                declared = [str(run_root / path) for path in downloaded]
                manifest_source = "registered_run_download_manifest"
            files = {}
            for raw in declared:
                path = Path(str(raw))
                if not path.is_absolute():
                    candidate = control / path
                    path = candidate if candidate.is_relative_to(root) else root / path
                if not path.is_relative_to(root) or ".." in path.parts:
                    raise ValueError("artifact_path_invalid")
                relative = path.relative_to(control).as_posix()
                reference = hashlib.sha256((parsed.identifier + ":" + relative).encode()).hexdigest()
                files[reference] = (path, relative)
            if parsed.operation == "resolve":
                if parsed.reference not in files:
                    raise ValueError("artifact_reference_unavailable")
                source, relative = files[parsed.reference]
                # Use the same policy/approval callback as ordinary read calls.
                # No callback means no authority to read the control workspace.
                if not callable(extra.get("__permission_ask__")):
                    raise ValueError("artifact_read_authorization_unavailable")
                if not await context.ask("read", [source.as_posix(), "/workspace/" + relative]):
                    raise ValueError("artifact_read_denied")
            def query():
                if parsed.operation == "resolve":
                    if parsed.reference not in files:
                        raise ValueError("artifact_reference_unavailable")
                    source, relative = files[parsed.reference]
                    destination = "outputs/artifact_query/" + parsed.reference + "/" + source.name
                    execution = Path(str(extra.get("workdir") or control)).resolve()
                    from ..path_policy import resolve_workspace_io_path
                    decision = resolve_workspace_io_path("/workspace/" + destination, context, tool_id="read", operation="read")
                    if decision.blocked or Path(decision.host_path).resolve() != execution / destination:
                        raise ValueError("artifact_read_not_allowed")
                    _import_declared_file(control, relative, execution, destination)
                    return {"reference": parsed.reference, "path": "/workspace/" + destination}
                extension = parsed.extension.lower().lstrip(".")
                entries = [{"reference": ref, "name": path.name,
                            "relative_path": path.relative_to(root).as_posix()}
                           for ref, (path, _) in sorted(files.items(), key=lambda item: item[1][1])
                           if not extension or path.name.lower().endswith("." + extension)]
                end = parsed.offset + parsed.limit
                return {"items": entries[parsed.offset:end], "total": len(entries),
                        "next_offset": end if end < len(entries) else None,
                        "complete": True, "source": manifest_source}
            result = await asyncio.to_thread(query)
            return ToolResult(title="任务产物", output=json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            safe = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("artifact_") else "artifact_query_failed"
            if isinstance(exc, artifact_transport.ArtifactMaterializationError):
                safe = ("artifact_target_conflict" if str(exc) == "artifact_target_conflict" else
                        "artifact_download_limit" if str(exc).startswith("Selected artifacts exceed") else
                        "artifact_download_failed")
            messages = {
                "artifact_file_missing": "声明的产物文件已缺失，无法导入；请检查原任务的产物回传状态，不要重新提交计算。",
                "artifact_target_conflict": "目标文件已变化或发生冲突，已保留现有文件；请先检查当前文件，不要强制覆盖。",
                "artifact_target_unavailable": "产物导入目标目录不可用或已被移除，源产物未被修改；请检查当前工作区后重试。",
                "artifact_read_denied": "没有获准读取该产物，未读取或导入文件。",
                "artifact_read_authorization_unavailable": "读取授权机制不可用，未读取或导入文件。",
                "artifact_download_limit": "所选文件超过当前下载保护阈值，远端文件仍保留；请调整文件传输配置或使用已有远端分析能力，不要重跑计算。",
                "artifact_download_failed": "所选产物下载或指纹校验未完成，未发布不完整文件；请检查文件服务及清单是否变化，不要重新提交计算。",
            }
            metadata = {"error_code": safe}
            if safe == "artifact_read_denied":
                metadata["permission_denied"] = True
                metadata["terminal_tool_error"] = True
            return ToolResult(title="任务产物", output="", error=messages.get(safe, safe), metadata=metadata)

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry
        ToolRegistry.register(cls())
