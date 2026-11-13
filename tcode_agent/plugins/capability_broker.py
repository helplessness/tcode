"""隔离插件反向调用 Host 能力的唯一授权代理。"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools.base import ToolContext
from .manifest import PluginManifest
from .permissions import (
    PluginPermission,
    PluginPermissionError,
    PluginPermissionScope,
    PluginRuntime,
    check_permission,
)
from .embedding_api import PluginEmbeddingAPI


@dataclass(frozen=True, slots=True)
class PluginInvocationLease:
    """保存一次活动插件工具调用的 Host 权威身份。"""

    request_id: str
    tool_id: str
    side_effect_level: str
    context: ToolContext
    artifacts: Any = None


class PluginCapabilityBroker:
    """校验插件权限并把请求委派给现有 Tool 与 Sandbox 管线。"""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        permissions: tuple[PluginPermission, ...],
        workdir: str,
    ) -> None:
        """绑定已校验 Manifest、有效授权和真实工作目录。"""

        self.manifest = manifest
        self.permissions = list(permissions)
        self.workdir = workdir
        self._plugin_tool_ids: frozenset[str] = frozenset()

    def bind_plugin_tools(self, tool_ids: list[str]) -> None:
        """在初始化完成后冻结本插件工具集合，用于递归调用拒绝。"""

        self._plugin_tool_ids = frozenset(
            str(item).strip() for item in tool_ids if str(item).strip()
        )

    def _allowed(self, scope: PluginPermissionScope, target: str) -> bool:
        """判断目标是否命中 Host 已授予的精确权限模式。"""

        return check_permission(
            self.permissions,
            scope.value,
            str(target),
            workdir=self.workdir,
        )

    def _require(self, scope: PluginPermissionScope, target: str) -> None:
        """在能力委派前失败关闭未获授权的目标。"""

        if not self._allowed(scope, target):
            raise PluginPermissionError(
                f"plugin permission denied: scope={scope.value} target={target}"
            )

    @staticmethod
    def _require_effect(lease: PluginInvocationLease, required: str) -> None:
        """阻止伪装为纯工具的插件在运行期执行隐藏副作用。"""

        order = {"none": 0, "read": 1, "write": 2, "process": 3}
        if order.get(lease.side_effect_level, 0) < order.get(required, 3):
            raise PluginPermissionError(
                "plugin tool side_effect_level is too narrow for delegated capability"
            )

    async def invoke(
        self,
        params: Mapping[str, Any],
        lease: PluginInvocationLease,
    ) -> dict[str, Any]:
        """分发一项反向能力请求，并保持父调用的超时与取消边界。"""

        capability = str(params.get("capability") or "").strip()
        if capability in {"artifacts.read_text", "artifacts.publish_json"}:
            if lease.artifacts is None:
                raise PluginPermissionError("artifact capability is unavailable")
            if capability == "artifacts.read_text":
                self._require_effect(lease, "read")
                return await lease.artifacts.read_text_page(str(params.get("path") or ""),
                    token=str(params.get("continuation") or ""),
                    sha256=str(params.get("sha256") or ""), snapshot=params.get("snapshot") is True,
                    max_bytes=params.get("max_bytes", 192 * 1024))
            self._require_effect(lease, "write")
            return await lease.artifacts.publish_json(params.get("value"))
        if capability == "process.run":
            return await self._invoke_process(params, lease)
        if capability == "network.request":
            return await self._invoke_network(params, lease)
        if capability == "tools.invoke":
            return await self._invoke_tool(params, lease)
        if capability == "embeddings.describe":
            return await self._invoke_embeddings_describe(lease)
        if capability == "embeddings.embed":
            return await self._invoke_embeddings_embed(params, lease)
        if capability == "ontology.read_facts":
            return self._invoke_ontology_read_facts(params, lease)
        if capability == "ontology.query":
            return self._invoke_ontology_query(params, lease)
        if capability == "ontology.validate_report":
            return self._invoke_ontology_validate_report(params, lease)
        raise PluginPermissionError(f"unsupported plugin capability: {capability}")

    def _ontology_capability(self, lease: PluginInvocationLease):
        """Issue the same per-call managed-workspace capability as host adapters."""

        from ..ontology_host import read_capability

        self._require(PluginPermissionScope.ONTOLOGY_READ, "workspace")
        extra = lease.context.extra if isinstance(lease.context.extra, Mapping) else {}
        return read_capability(str(extra.get("workdir") or self.workdir))

    def _invoke_ontology_read_facts(
        self, params: Mapping[str, Any], lease: PluginInvocationLease
    ) -> dict[str, Any]:
        self._require_effect(lease, "read")
        capability = self._ontology_capability(lease)
        try:
            return dict(capability.read_facts(list(params.get("references") or [])))
        finally:
            capability.close()

    def _invoke_ontology_query(
        self, params: Mapping[str, Any], lease: PluginInvocationLease
    ) -> dict[str, Any]:
        self._require_effect(lease, "read")
        capability = self._ontology_capability(lease)
        try:
            return dict(capability.query(**dict(params.get("arguments") or {})))
        finally:
            capability.close()

    def _invoke_ontology_validate_report(
        self, params: Mapping[str, Any], lease: PluginInvocationLease
    ) -> dict[str, Any]:
        self._require_effect(lease, "read")
        capability = self._ontology_capability(lease)
        try:
            return dict(capability.validate_report(list(params.get("claims") or [])))
        finally:
            capability.close()

    def _embedding_api(self) -> PluginEmbeddingAPI:
        self._require(PluginPermissionScope.MODEL_EMBEDDING, "configured")
        return PluginEmbeddingAPI(self.workdir, self.permissions)

    async def _invoke_embeddings_describe(
        self, lease: PluginInvocationLease
    ) -> dict[str, Any]:
        self._require_effect(lease, "read")
        return dict(await self._embedding_api().describe())

    async def _invoke_embeddings_embed(
        self, params: Mapping[str, Any], lease: PluginInvocationLease
    ) -> dict[str, Any]:
        self._require_effect(lease, "read")
        return dict(await self._embedding_api().embed(
            list(params.get("texts") or []),
            expected_signature=str(params.get("expected_signature") or ""),
            kind=str(params.get("kind") or "document"),
        ))

    async def _invoke_process(
        self,
        params: Mapping[str, Any],
        lease: PluginInvocationLease,
    ) -> dict[str, Any]:
        """通过既有 PluginRuntime 和 tcode-executor 执行命令。"""

        self._require_effect(lease, "process")
        command = str(params.get("command") or "").strip()
        profile = str(params.get("profile") or "").strip()
        timeout = max(1, min(int(params.get("timeout") or 60), 3600))
        requested_cwd = str(params.get("cwd") or "").strip()
        if requested_cwd not in {"", ".", "plugin"}:
            raise PluginPermissionError(
                "isolated plugin process cwd must be its staged package"
            )
        job_root = (
            Path(self.workdir)
            / ".tcode-runtime"
            / "plugin-exec"
            / uuid.uuid4().hex
        ).resolve()
        job_root.relative_to(Path(self.workdir).resolve())
        package_root = job_root / "plugin"
        job_root.mkdir(parents=True, exist_ok=False)
        try:
            if self.manifest.package_root is None:
                raise RuntimeError("plugin package root is unavailable")
            shutil.copytree(
                self.manifest.package_root,
                package_root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            runtime = PluginRuntime(
                workdir=job_root.as_posix(),
                permissions=self.permissions,
                agent_name=self.manifest.name,
                require_sandbox=True,
            )
            result = await asyncio.wait_for(
                runtime.run_command(
                    command,
                    [str(item) for item in list(params.get("args") or [])],
                    cwd=None,
                    timeout=timeout,
                    sandbox_profile=profile or None,
                    host_context=lease.context,
                ),
                timeout=timeout + 10,
            )
            result["staged_workspace"] = True
            result.pop("cwd", None)
            return dict(result)
        finally:
            shutil.rmtree(job_root, ignore_errors=True)

    async def _invoke_network(
        self,
        params: Mapping[str, Any],
        lease: PluginInvocationLease,
    ) -> dict[str, Any]:
        """通过固定 networked Profile 和 Egress Proxy 请求网络。"""

        self._require_effect(lease, "read")
        timeout = max(1, min(int(params.get("timeout") or 30), 300))
        job_root = (
            Path(self.workdir)
            / ".tcode-runtime"
            / "plugin-network"
            / uuid.uuid4().hex
        ).resolve()
        job_root.relative_to(Path(self.workdir).resolve())
        job_root.mkdir(parents=True, exist_ok=False)
        try:
            runtime = PluginRuntime(
                workdir=job_root.as_posix(),
                permissions=self.permissions,
                agent_name=self.manifest.name,
                require_sandbox=True,
            )
            result = await asyncio.wait_for(
                runtime.request_network(
                    str(params.get("method") or "GET"),
                    str(params.get("url") or ""),
                    headers={
                        str(key): str(value)
                        for key, value in dict(params.get("headers") or {}).items()
                    },
                    body=(
                        str(params.get("body"))
                        if params.get("body") is not None
                        else None
                    ),
                    timeout=timeout,
                    host_context=lease.context,
                ),
                timeout=timeout + 15,
            )
            return dict(result)
        finally:
            shutil.rmtree(job_root, ignore_errors=True)

    async def _invoke_tool(
        self,
        params: Mapping[str, Any],
        lease: PluginInvocationLease,
    ) -> dict[str, Any]:
        """重新进入 AgentToolExecutor，调用已授权 Tool 或 Skill。"""

        self._require_effect(lease, "process")
        tool_id = str(params.get("tool_id") or "").strip()
        if not tool_id:
            raise PluginPermissionError("plugin delegated tool id is required")
        if tool_id in self._plugin_tool_ids:
            raise PluginPermissionError("plugin cannot recursively invoke its own tool")
        self._require(PluginPermissionScope.TOOLS_INVOKE, tool_id)
        arguments = dict(params.get("arguments") or {})
        if tool_id == "skill_run":
            skill_name = str(arguments.get("skill") or "").strip()
            if not skill_name:
                raise PluginPermissionError("delegated skill_run requires a skill name")
            self._require(PluginPermissionScope.SKILLS_INVOKE, skill_name)
        extra = lease.context.extra if isinstance(lease.context.extra, dict) else {}
        callback = extra.get("__plugin_tool_invoke__")
        if not callable(callback):
            raise PluginPermissionError(
                "delegated Host tools require an active AgentToolExecutor invocation"
            )
        timeout = max(1, min(int(params.get("timeout") or 300), 3600))
        result = callback(
            tool_id,
            arguments,
            source_plugin=self.manifest.name,
            parent_call_id=str(lease.context.call_id or lease.request_id),
        )
        if hasattr(result, "__await__"):
            result = await asyncio.wait_for(result, timeout=timeout)
        if not isinstance(result, Mapping):
            raise RuntimeError("delegated Host tool returned an invalid result")
        return dict(result)


__all__ = ["PluginCapabilityBroker", "PluginInvocationLease"]
