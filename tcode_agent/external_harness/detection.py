"""有超时和故障隔离的 runtime 探测。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from .runtime_def import HarnessRuntimeDef, HarnessRuntimeRecord, RuntimeProbeResult
from .security import minimal_environment


async def _run_probe(executable: str, args: list[str], timeout: float) -> tuple[int, str, str]:
    import os
    if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
        return 1, '', '桌面外部 CLI 探测尚未接入原生隔离'
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=minimal_environment(),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout[:2_000_000].decode(errors="replace"), stderr[:128_000].decode(errors="replace")


async def probe_runtime(
    runtime: HarnessRuntimeDef,
    *,
    enabled: bool,
    include_models: bool = False,
    record: HarnessRuntimeRecord | None = None,
) -> RuntimeProbeResult:
    candidates = [runtime.executables.primary, *runtime.executables.fallbacks]
    resolved = ""
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            resolved = Path(found).resolve(strict=True).as_posix()
            break
    diagnostics: list[str] = []
    version = ""
    models: list[str] = []
    auth = "unknown"
    if not resolved:
        diagnostics.append(f"未找到可执行文件：{runtime.executables.primary}")
    else:
        try:
            code, stdout, stderr = await _run_probe(
                resolved,
                runtime.probes.version_args,
                runtime.probes.timeout_seconds,
            )
            version = (stdout or stderr).strip().splitlines()[0][:200] if (stdout or stderr).strip() else ""
            if code != 0:
                diagnostics.append(f"版本探测退出码 {code}")
        except asyncio.TimeoutError:
            diagnostics.append("版本探测超时")
        except Exception as exc:
            diagnostics.append(f"版本探测失败：{str(exc)[:300]}")

        if include_models and runtime.probes.model_strategy == "opencode-models":
            try:
                code, stdout, stderr = await _run_probe(resolved, ["models"], 10.0)
                if code == 0:
                    models = [line.strip()[:300] for line in stdout.splitlines() if line.strip()][:5000]
                    auth = "configured" if models else "unknown"
                else:
                    diagnostics.append(f"模型探测退出码 {code}：{stderr.strip()[:200]}")
            except asyncio.TimeoutError:
                diagnostics.append("模型探测超时")
            except Exception as exc:
                diagnostics.append(f"模型探测失败：{str(exc)[:300]}")

    capabilities = runtime.capabilities.model_dump(mode="json", by_alias=True)
    if record is not None and record.source_type != "builtin":
        verified = set(record.capabilities_verified)
        declared = dict(capabilities)
        for name in ("text", "images", "tools", "resume", "cancel"):
            capabilities[name] = bool(
                getattr(runtime.capabilities, name) and name in verified
            )
        if not capabilities["tools"]:
            capabilities["mcpInjection"] = "none"
            capabilities["permissionAuthority"] = "none"
        if capabilities.get("guidanceMode") == "restart_turn" and not (
            capabilities["cancel"] and capabilities["resume"]
        ):
            capabilities["guidanceMode"] = "queue_only"
        if capabilities.get("guidanceMode") == "in_turn":
            capabilities["guidanceMode"] = "queue_only"
        capabilities["declaredCapabilities"] = declared
        capabilities["verifiedCapabilities"] = sorted(verified)
        capabilities["conformanceState"] = record.conformance_state
    compatible = record is None or record.compatibility_state == "compatible"
    trusted = record is None or record.trust_state not in {"untrusted", "rejected"}
    if record is not None:
        diagnostics.extend(
            item for item in record.diagnostics if item not in diagnostics
        )
    return RuntimeProbeResult(
        runtimeId=runtime.id,
        displayName=runtime.display_name,
        release=runtime.release,
        available=bool(resolved and compatible and trusted),
        enabled=bool(enabled and compatible and trusted),
        executable=resolved if os.environ.get("TCODE_EXPOSE_HARNESS_PATHS") == "1" else Path(resolved).name if resolved else "",
        version=version,
        auth=auth,
        models=models,
        capabilities=capabilities,
        diagnostics=diagnostics,
        sourceType=record.source_type if record else "builtin",
        sourceName=record.source_name if record else "tcode-agent",
        distributionName=record.distribution_name if record else "",
        distributionVersion=record.distribution_version if record else "",
        definitionDigest=record.definition_digest if record else "",
        trustState=record.trust_state if record else "builtin",
        compatibilityState=(
            record.compatibility_state if record else "compatible"
        ),
        conformanceState=record.conformance_state if record else "builtin",
        capabilitiesVerified=(
            record.capabilities_verified if record else []
        ),
    )


__all__ = ["probe_runtime"]
