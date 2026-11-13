"""Input transport contracts; pointers are relative to target args.

Boltz/PLIP/GNINA use broker-published metadata pinned to one ToolContext.
The local table remains for unmigrated tools and explicit legacy queue recovery,
never as a fallback when a migrated tool's publication is unavailable.
"""
from __future__ import annotations

from typing import Any
from copy import deepcopy
import hashlib
import json
import re


PUBLISHED_INPUT_TARGETS = frozenset({
    ("boltz-mcp", "boltz_mcp_server"),
    ("plip-mcp", "plip_mcp_server"),
    ("gnina-mcp", "gnina_mcp_server"),
})


def input_file_contract(context, server: str, tool: str) -> dict | None:
    contracts = getattr(context, "_mcp_file_contracts", {})
    return deepcopy(contracts.get((server.lower(), tool)))


def pin_input_file_contract(context, server: str, tool: str, payload: dict) -> dict:
    """Accept only broker metadata, never user tool arguments or context.extra."""
    server = server.lower()
    if context is None:
        raise ValueError("文件声明缺少调用上下文，未上传或入队")
    if not isinstance(payload, dict) or payload.get("ok") is not True or (
        payload.get("target_server"), payload.get("target_tool")
    ) != (server, tool):
        raise ValueError("TMCP文件声明目标不匹配或不可用，未上传或入队")
    files = payload.get("files")
    if not isinstance(files, list) or len(files) > 256:
        raise ValueError("TMCP文件声明缺失或损坏，请更新Job Protocol；不能重新猜测文件")
    pointers = set()
    for field in files:
        if not isinstance(field, dict):
            raise ValueError("TMCP文件声明字段无效")
        if field.keys() - {"pointer", "kind", "transport"}:
            raise ValueError("TMCP文件声明含额外字段，未读取文件或入队")
        pointer = field.get("pointer")
        if (not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 2048
            or re.search(r"~(?![01])", pointer)
            or field.get("kind") not in {"file", "directory", "file_or_directory"}
            or field.get("transport") != "artifact" or pointer in pointers):
            raise ValueError("TMCP文件声明字段无效或重复")
        pointers.add(pointer)
    encoded = json.dumps({"files": files}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    if len(encoded.encode()) > 65536 or payload.get("files_hash") != digest:
        raise ValueError("TMCP文件声明摘要不匹配或超限，未上传或入队")
    contract = {"ok": True, "target_server": server, "target_tool": tool,
                "files": deepcopy(files), "files_hash": digest,
                "schema_hash": str(payload.get("schema_hash") or ""),
                "source": "tmcp.describe_target_tool"}
    contracts = dict(getattr(context, "_mcp_file_contracts", {}))
    existing = contracts.get((server, tool))
    if existing is not None:
        return deepcopy(existing)  # One invocation never switches declarations midway.
    contracts[(server, tool)] = contract
    context._mcp_file_contracts = contracts
    return deepcopy(contract)


async def prepare_input_file_contract(server: str, tool: str, arguments: dict, context) -> None:
    server, tool, _, _ = _target_arguments(server, tool, arguments)
    if (server, tool) not in PUBLISHED_INPUT_TARGETS or input_file_contract(context, server, tool) is not None:
        return
    if context is None:
        raise ValueError("文件声明缺少调用上下文，未上传或入队")
    from ..mcp.manager_auto import MCPManager
    from ..mcp._proxy import _extract_contract_payload
    try:
        if not MCPManager.is_initialized():
            await MCPManager.initialize()
        raw = await MCPManager.call_tool("job-protocol-mcp", "describe_target_tool", {"input": {
            "target_server": server, "target_tool": tool, "detail": "compact", "fields": [],
        }})
    except Exception as exc:
        raise ValueError(f"无法取得TMCP文件声明（{type(exc).__name__}），请检查Job Protocol连接与部署；"
                         "未上传或入队，不会回退猜测") from exc
    pin_input_file_contract(context, server, tool, _extract_contract_payload(raw))


NATIVE_INPUT_FILES = {
    ("boltz-mcp", "boltz_mcp_server"): (
        {"pointer": "/tools_input", "kind": "file_or_directory"},
        {"pointer": "/checkpoint", "kind": "file"},
        {"pointer": "/affinity_checkpoint", "kind": "file"},
    ),
    ("plip-mcp", "plip_mcp_server"): (
        {"pointer": "/file", "kind": "file"},
        {"pointer": "/files", "kind": "file"},
    ),
    ("gnina-mcp", "gnina_mcp_server"): tuple(
        {"pointer": f"/{name}", "kind": "file"}
        for name in ("receptor", "ligand", "flex", "flexdist_ligand", "autobox_ligand",
                     "custom_scoring", "custom_atoms", "user_grid", "cnn_model")
    ),
    ("pythia-mcp", "pythia_mcp_server"): ({"pointer": "/pdb", "kind": "file"},),
    ("foldx-mcp", "foldx_repair_mcp_server"): ({"pointer": "/pdb", "kind": "file"},),
    ("foldx-mcp", "foldx_scan_mcp_server"): ({"pointer": "/pdb", "kind": "file"},),
    ("homology-mcp", "homology_search_mcp_server"): ({"pointer": "/fasta", "kind": "file"},),
    ("homology-mcp", "homology_align_mcp_server"): ({"pointer": "/fasta", "kind": "file"},),
    ("homology-mcp", "homology_conservation_mcp_server"): ({"pointer": "/alignment", "kind": "file"},),
    ("mutation-scoring-mcp", "multi_model_sequence_mutation_scoring_mcp_server"): (
        {"pointer": "/fasta_path", "kind": "file"},),
    ("simulation-mcp", "simulation_run_mcp_server"): tuple(
        {"pointer": f"/{name}", "kind": "file"} for name in ("protein", "ligand")),
    ("placer-mcp", "placer_mcp_server"): (
        {"pointer": "/idir", "kind": "directory"},
        *({"pointer": f"/{name}", "kind": "file"}
          for name in ("ifile", "ligand_file", "weights", "residue_json"))),
    ("rfdiffusion-mcp", "rfdiffusion_mcp_server"): (
        {"pointer": "/tools_input", "kind": "file_or_directory"},
        {"pointer": "/inference_ckpt_override_path", "kind": "file"},
        {"pointer": "/scaffoldguided_target_path", "kind": "file"},
        {"pointer": "/scaffoldguided_scaffold_dir", "kind": "directory"}),
    ("ligandmpnn-mcp", "ligandmpnn_run_mcp_server"): tuple(
        {"pointer": f"/{name}", "kind": "file"} for name in (
            "tools_input", "pdb_path_multi", "fixed_residues_multi", "redesigned_residues_multi",
            "bias_AA_per_residue", "bias_AA_per_residue_multi", "omit_AA_per_residue",
            "omit_AA_per_residue_multi", "checkpoint_protein_mpnn", "checkpoint_ligand_mpnn",
            "checkpoint_per_residue_label_membrane_mpnn", "checkpoint_global_label_membrane_mpnn",
            "checkpoint_soluble_mpnn", "checkpoint_path_sc")),
    **{("evolvepro-mcp", tool): tuple({"pointer": f"/{name}", "kind": "file"} for name in (
        "excel_files", "excel_file_multi", "mult_file", "wt_fasta_path", "mutants_fasta_path",
        "precomputed_embedding_csv", "precomputed_embedding_csvs"))
       for tool in ("evolvepro_single_round_mcp_server", "evolvepro_multi_round_mcp_server")},
    ("simple-demo-mcp", "echo_text"): (),
    ("simple-demo-mcp", "sleep_then_echo"): (),
}


def _target_arguments(server, tool, arguments):
    prefix: tuple = ()
    values = arguments
    server = str(server).strip().lower()
    tool = str(tool).strip()
    if server == "job-protocol-mcp":
        if tool != "submit_job":
            return server, tool, values, prefix
        while isinstance(values, dict) and not values.get("target_server"):
            key = next((key for key in ("input", "input_config", "payload", "arguments")
                        if isinstance(values.get(key), dict)), None)
            if key is None:
                return server, tool, values, prefix
            values, prefix = values[key], (*prefix, key)
        server = str(values.get("target_server", "")).strip().lower()
        tool = str(values.get("target_tool", "")).strip()
        if isinstance(values.get("arguments"), dict):
            values, prefix = values["arguments"], (*prefix, "arguments")
    if not server.startswith('plugin-') and isinstance(values.get("input_config"), dict):
        values, prefix = values["input_config"], (*prefix, "input_config")
    return server, tool, values, prefix


def declared_resource_paths(
    server: str, tool: str, arguments: dict[str, Any], *, context=None, legacy=False,
) -> dict[tuple, str] | None:
    """Parse one pinned declaration for both snapshots and transport."""
    server, tool, values, prefix = _target_arguments(server, tool, arguments)
    fields = NATIVE_INPUT_FILES.get((server, tool))
    if (server, tool) in PUBLISHED_INPUT_TARGETS and not legacy:
        contract = input_file_contract(context, server, tool)
        if contract is None:
            raise ValueError("TMCP文件声明尚未加载，不能按静态表或关键词猜测输入")
        fields = contract["files"]
    if server.startswith("plugin-"):
        from ..plugins.async_targets import find_target
        from ..mcp._utils import _context_workspace_claims
        from .async_protein_run import _context_control_workdir
        claims = _context_workspace_claims(context)
        owner = f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}"
        from ..mcp.custom_targets import pinned_target
        target = pinned_target(context, server) or find_target(_context_control_workdir(context), server, owner=owner)
        if target is None:
            raise ValueError("插件异步目标未启用或不属于当前工作区")
        if target.get('origin') != 'mcp-config':
            tool = str(values.get("target_tool") or tool)
        entry = next((entry for entry in target["tools"] if entry["name"] == tool), None)
        if entry is None:
            raise ValueError("插件未声明此异步工具")
        fields = entry.get("files", [])
        if target.get('origin') != 'mcp-config':
            if isinstance(values.get('input_config'), dict):
                values, prefix = values['input_config'], (*prefix, 'input_config')
            if isinstance(values.get("arguments"), dict):
                values, prefix = values["arguments"], (*prefix, "arguments")
    if fields is None:
        return None
    result: dict[tuple, str] = {}

    def collect(value: Any, path: tuple, kind: str) -> None:
        if value is None or value == "":
            return  # Optional fields keep their existing empty-value behavior.
        if isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, (*path, index), kind)
        elif isinstance(value, dict):
            ref_kind = value.get("kind")
            if ref_kind == "artifact" and "artifact_id" in value:
                if not isinstance(value["artifact_id"], str) or not value["artifact_id"].strip():
                    raise ValueError("产物引用缺少有效的 artifact_id")
                if kind == "directory":
                    raise ValueError("目录字段不能传入单文件引用")
                return
            if ref_kind in {"artifact_bundle", "bundle"} and "bundle_id" in value:
                if not isinstance(value["bundle_id"], str) or not value["bundle_id"].strip():
                    raise ValueError("目录引用缺少有效的 bundle_id")
                if kind == "file":
                    raise ValueError("文件字段不能传入目录引用")
                return
            if ref_kind == "artifact" and isinstance(value.get("path"), str) and value["path"].strip():
                collect(value["path"], (*path, "path"), kind)
            else:
                raise ValueError("输入文件必须是路径或有效的产物引用")
        elif isinstance(value, str):
            result[path] = kind
        else:
            raise ValueError("输入文件必须是路径或有效的产物引用")

    for field in fields:
        parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in field["pointer"][1:].split("/"))
        value = values
        try:
            for part in parts:
                value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # Required/type checks remain the tool contract's job.
        collect(value, (*prefix, *parts), field["kind"])
    return result
