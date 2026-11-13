"""分层 Runtime Registry：内置、声明式 Profile 与 Python entry point。"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from .compatibility import definition_digest, evaluate_compatibility
from .conformance.store import HarnessConformanceStore
from .detection import probe_runtime
from .discovery import discover_entry_points, discover_profile_files
from .profile_loader import load_profile
from .provider import instantiate_provider, provider_definition
from .runtime_def import (
    HarnessRuntimeDef,
    HarnessRuntimeRecord,
    RuntimeProbeResult,
)
from .runtimes import builtin_runtime_defs
from .trust_store import HarnessTrustStore, default_settings_path


_RUNTIME_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")




class HarnessRuntimeRegistry:
    def __init__(
        self,
        *,
        discover_external: bool | None = None,
        trust_store: HarnessTrustStore | None = None,
    ) -> None:
        self.trust_store = trust_store or HarnessTrustStore()
        self.conformance_store = HarnessConformanceStore(
            self.trust_store.path.parent
        )
        self._defs: dict[str, HarnessRuntimeDef] = {}
        self._accepted_records: dict[str, HarnessRuntimeRecord] = {}
        self._records: list[HarnessRuntimeRecord] = []
        self._providers: dict[str, Any] = {}
        self._entrypoint_sources: dict[str, str] = {}
        self._discovery_complete = False
        for definition in builtin_runtime_defs():
            self.register(
                definition,
                source_type="builtin",
                source_name="tcode-agent",
            )
        should_discover = (
            str(os.environ.get("TCODE_EXTERNAL_HARNESS_DISCOVERY", "1")).lower()
            not in {"0", "false", "no", "off"}
            if discover_external is None
            else bool(discover_external)
        )
        if should_discover:
            self.discover()

    def register(
        self,
        definition: HarnessRuntimeDef,
        *,
        source_type: str = "builtin",
        source_name: str = "programmatic",
        distribution_name: str = "",
        distribution_version: str = "",
        trust_state: str = "builtin",
        provider: Any = None,
    ) -> None:
        if definition.id in self._defs:
            raise ValueError(f"duplicate runtime id: {definition.id}")
        compatibility = evaluate_compatibility(definition)
        digest = definition_digest(definition)
        if source_type == "builtin":
            conformance_state = "builtin"
            verified = sorted(
                name
                for name in ("text", "images", "tools", "resume", "cancel")
                if bool(getattr(definition.capabilities, name))
            )
        else:
            conformance_state, verified = self.conformance_store.read(
                definition.id,
                definition_digest=digest,
            )
        enabled = self.is_enabled(definition.id)
        diagnostics = list(compatibility.diagnostics)
        if source_type == "profile":
            expected_digest = self.trust_store.enabled_definition_digest(
                definition.id
            )
            enabled = bool(
                self.trust_store.is_enabled(definition.id)
                and expected_digest == digest
            )
            if (
                self.trust_store.is_enabled(definition.id)
                and expected_digest
                and expected_digest != digest
            ):
                diagnostics.append(
                    "profile definition changed; validate and enable it again"
                )
        record = HarnessRuntimeRecord(
            runtimeId=definition.id,
            displayName=definition.display_name,
            sourceType=source_type,
            sourceName=source_name,
            distributionName=distribution_name,
            distributionVersion=distribution_version,
            definitionDigest=digest,
            trustState=trust_state,
            enabled=enabled,
            compatibilityState=compatibility.state,
            diagnostics=diagnostics,
            conformanceState=conformance_state,
            capabilitiesVerified=verified,
            definition=definition,
        )
        self._defs[definition.id] = definition
        self._accepted_records[definition.id] = record
        self._records.append(record)
        if provider is not None:
            self._providers[definition.id] = provider

    def _add_discovered(
        self,
        *,
        definition: HarnessRuntimeDef,
        source_type: str,
        source_name: str,
        distribution_name: str = "",
        distribution_version: str = "",
        trust_state: str,
        provider: Any = None,
    ) -> None:
        compatibility = evaluate_compatibility(definition)
        if definition.id in self._defs:
            existing = self._accepted_records[definition.id]
            self._records.append(
                HarnessRuntimeRecord(
                    runtimeId=definition.id,
                    displayName=definition.display_name,
                    sourceType=source_type,
                    sourceName=source_name,
                    distributionName=distribution_name,
                    distributionVersion=distribution_version,
                    definitionDigest=definition_digest(definition),
                    trustState="rejected",
                    enabled=False,
                    compatibilityState="conflict",
                    diagnostics=[
                        f"runtime id conflicts with {existing.source_type}:"
                        f"{existing.source_name}"
                    ],
                    definition=definition,
                )
            )
            return
        self.register(
            definition,
            source_type=source_type,
            source_name=source_name,
            distribution_name=distribution_name,
            distribution_version=distribution_version,
            trust_state=trust_state,
            provider=provider,
        )
        accepted = self._accepted_records[definition.id]
        if compatibility.state != "compatible":
            updated = accepted.model_copy(update={"enabled": False})
            self._accepted_records[definition.id] = updated
            self._records[-1] = updated

    def _add_error_record(
        self,
        *,
        runtime_id: str,
        display_name: str,
        source_type: str,
        source_name: str,
        trust_state: str,
        compatibility_state: str,
        diagnostic: str,
        distribution_name: str = "",
        distribution_version: str = "",
        accepted: bool = False,
    ) -> None:
        record = HarnessRuntimeRecord(
            runtimeId=runtime_id[:200] or "invalid-profile",
            displayName=display_name[:200]
            or runtime_id[:200]
            or "Invalid Harness",
            sourceType=source_type,
            sourceName=source_name[:500] or "<unknown>",
            distributionName=distribution_name[:300],
            distributionVersion=distribution_version[:120],
            trustState=trust_state,
            enabled=False,
            compatibilityState=compatibility_state,
            diagnostics=[str(diagnostic)[:1000]],
        )
        self._records.append(record)
        if accepted and runtime_id not in self._accepted_records:
            self._accepted_records[runtime_id] = record

    def discover(self) -> list[HarnessRuntimeRecord]:
        if self._discovery_complete:
            return self.records()
        self._discovery_complete = True
        for path in discover_profile_files():
            try:
                loaded = load_profile(path)
            except Exception as exc:
                self._add_error_record(
                    runtime_id=path.stem,
                    display_name=path.stem,
                    source_type="profile",
                    source_name=path.as_posix(),
                    trust_state="rejected",
                    compatibility_state="error",
                    diagnostic=str(exc),
                )
                continue
            self._add_discovered(
                definition=loaded.definition,
                source_type="profile",
                source_name=loaded.path.as_posix(),
                trust_state="declarative",
            )

        for candidate in discover_entry_points():
            if not _RUNTIME_ID.fullmatch(candidate.runtime_id):
                self._add_error_record(
                    runtime_id=candidate.runtime_id,
                    display_name=candidate.runtime_id,
                    source_type="entrypoint",
                    source_name=candidate.source_key,
                    distribution_name=candidate.distribution_name,
                    distribution_version=candidate.distribution_version,
                    trust_state="rejected",
                    compatibility_state="error",
                    diagnostic="entry point name must be a valid runtime id",
                )
                continue
            if candidate.runtime_id in self._accepted_records:
                existing = self._accepted_records[candidate.runtime_id]
                self._add_error_record(
                    runtime_id=candidate.runtime_id,
                    display_name=candidate.runtime_id,
                    source_type="entrypoint",
                    source_name=candidate.source_key,
                    distribution_name=candidate.distribution_name,
                    distribution_version=candidate.distribution_version,
                    trust_state="rejected",
                    compatibility_state="conflict",
                    diagnostic=(
                        f"runtime id conflicts with {existing.source_type}:"
                        f"{existing.source_name}"
                    ),
                )
                continue
            self._entrypoint_sources[candidate.runtime_id] = candidate.source_key
            trusted = self.trust_store.is_entrypoint_trusted(candidate.source_key)
            enabled = self.is_enabled(candidate.runtime_id)
            if not trusted or not enabled:
                self._add_error_record(
                    runtime_id=candidate.runtime_id,
                    display_name=candidate.runtime_id,
                    source_type="entrypoint",
                    source_name=candidate.source_key,
                    distribution_name=candidate.distribution_name,
                    distribution_version=candidate.distribution_version,
                    trust_state="trusted" if trusted else "untrusted",
                    compatibility_state="unknown",
                    diagnostic=(
                        "entry point is disabled"
                        if trusted
                        else "entry point code is not loaded until explicitly enabled"
                    ),
                    accepted=True,
                )
                continue
            try:
                provider = instantiate_provider(candidate.point.load())
                definition = provider_definition(provider)
                if definition.id != candidate.runtime_id:
                    raise ValueError(
                        "provider runtime id does not match entry point name"
                    )
                self._add_discovered(
                    definition=definition,
                    source_type="entrypoint",
                    source_name=candidate.source_key,
                    distribution_name=candidate.distribution_name,
                    distribution_version=candidate.distribution_version,
                    trust_state="trusted",
                    provider=provider,
                )
            except Exception as exc:
                self._add_error_record(
                    runtime_id=candidate.runtime_id,
                    display_name=candidate.runtime_id,
                    source_type="entrypoint",
                    source_name=candidate.source_key,
                    distribution_name=candidate.distribution_name,
                    distribution_version=candidate.distribution_version,
                    trust_state="rejected",
                    compatibility_state="error",
                    diagnostic=f"provider load failed: {exc}",
                    accepted=True,
                )
        return self.records()

    def get(self, runtime_id: str) -> HarnessRuntimeDef | None:
        return self._defs.get(str(runtime_id or "").strip())

    def get_record(self, runtime_id: str) -> HarnessRuntimeRecord | None:
        return self._accepted_records.get(str(runtime_id or "").strip())

    def records(self) -> list[HarnessRuntimeRecord]:
        return sorted(
            self._records,
            key=lambda item: (item.runtime_id, item.source_type, item.source_name),
        )

    def definitions(self) -> list[HarnessRuntimeDef]:
        return [self._defs[key] for key in sorted(self._defs)]

    def provider_for(self, runtime_id: str) -> Any:
        return self._providers.get(str(runtime_id or "").strip())

    def effective_capabilities(self, runtime_id: str) -> dict[str, object]:
        definition = self.get(runtime_id)
        record = self.get_record(runtime_id)
        if definition is None or record is None:
            return {"available": False}
        declared = definition.capabilities.model_dump(mode="json", by_alias=True)
        if record.source_type == "builtin":
            effective = dict(declared)
        else:
            verified = set(record.capabilities_verified)
            effective = dict(declared)
            for field, alias in (
                ("text", "text"),
                ("images", "images"),
                ("tools", "tools"),
                ("resume", "resume"),
                ("cancel", "cancel"),
            ):
                effective[alias] = bool(
                    getattr(definition.capabilities, field)
                    and field in verified
                )
            if not effective["tools"]:
                effective["mcpInjection"] = "none"
                effective["permissionAuthority"] = "none"
            guidance = str(effective.get("guidanceMode") or "queue_only")
            if guidance == "restart_turn" and not (
                effective["cancel"] and effective["resume"]
            ):
                effective["guidanceMode"] = "queue_only"
            if guidance == "in_turn":
                effective["guidanceMode"] = "queue_only"
        return {
            **effective,
            "declaredCapabilities": declared,
            "verifiedCapabilities": list(record.capabilities_verified),
            "conformanceState": record.conformance_state,
        }

    def is_enabled(self, runtime_id: str) -> bool:
        explicitly = {
            item.strip()
            for item in str(
                os.environ.get("TCODE_HARNESS_ALLOWED_RUNTIME_IDS") or ""
            ).split(",")
            if item.strip()
        }
        if runtime_id in explicitly:
            return str(
                os.environ.get("TCODE_EXTERNAL_HARNESSES_ENABLED", "1")
            ).lower() not in {"0", "false", "no", "off"}
        enabled = self.trust_store.is_enabled(runtime_id)
        record = self._accepted_records.get(runtime_id)
        if enabled and record is not None and record.source_type == "profile":
            return (
                self.trust_store.enabled_definition_digest(runtime_id)
                == record.definition_digest
            )
        return enabled

    def set_enabled(self, runtime_id: str, enabled: bool) -> None:
        known = any(item.runtime_id == runtime_id for item in self._records)
        if not known:
            raise KeyError(runtime_id)
        source_key = self._entrypoint_sources.get(runtime_id, "") if enabled else ""
        record = self._accepted_records.get(runtime_id)
        self.trust_store.update(
            runtime_id=runtime_id,
            enabled=enabled,
            trusted_entrypoint=source_key,
            definition_digest=(
                record.definition_digest
                if enabled and record and record.source_type == "profile"
                else ""
            ),
        )
        current = self._accepted_records.get(runtime_id)
        if current is not None:
            updated = current.model_copy(update={"enabled": enabled})
            self._accepted_records[runtime_id] = updated
            for index, item in enumerate(self._records):
                if item is current:
                    self._records[index] = updated
                    break

    def allowed_for_request(
        self, runtime_id: str, *, public_server: bool
    ) -> tuple[bool, str]:
        definition = self.get(runtime_id)
        record = self.get_record(runtime_id)
        if definition is None or record is None:
            return False, "runtime is not registered"
        if record.compatibility_state != "compatible":
            return False, "runtime is incompatible with this TCode version"
        if record.trust_state in {"untrusted", "rejected"}:
            return False, "runtime provider is not trusted"
        if not self.is_enabled(runtime_id):
            return False, "runtime is installed but not enabled"
        authority = definition.capabilities.permission_authority
        if public_server and (
            not definition.security.public_server
            or authority not in {"tcode", "shared"}
        ):
            return (
                False,
                f"permissionAuthority={authority} is unavailable on a public server",
            )
        return True, ""

    def record_backend_error(self, runtime_id: str, error: Exception) -> None:
        record = self._accepted_records.get(runtime_id)
        if record is None:
            return
        diagnostics = [*record.diagnostics, f"backend creation failed: {error}"][-32:]
        updated = record.model_copy(
            update={
                "enabled": False,
                "compatibility_state": "error",
                "diagnostics": diagnostics,
            }
        )
        self._accepted_records[runtime_id] = updated
        for index, item in enumerate(self._records):
            if item is record:
                self._records[index] = updated
                break

    async def probes(
        self, *, include_models: bool = False
    ) -> list[RuntimeProbeResult]:
        tasks = []
        unresolved: list[RuntimeProbeResult] = []
        for record in self.records():
            if self._accepted_records.get(record.runtime_id) is not record:
                continue
            if record.definition is None:
                unresolved.append(
                    RuntimeProbeResult(
                        runtimeId=record.runtime_id,
                        displayName=record.display_name,
                        release="experimental",
                        available=False,
                        enabled=False,
                        capabilities={},
                        diagnostics=record.diagnostics,
                        sourceType=record.source_type,
                        sourceName=record.source_name,
                        distributionName=record.distribution_name,
                        distributionVersion=record.distribution_version,
                        definitionDigest=record.definition_digest,
                        trustState=record.trust_state,
                        compatibilityState=record.compatibility_state,
                        conformanceState=record.conformance_state,
                        capabilitiesVerified=record.capabilities_verified,
                    )
                )
                continue
            tasks.append(
                probe_runtime(
                    record.definition,
                    enabled=self.is_enabled(record.runtime_id),
                    include_models=include_models,
                    record=record,
                )
            )
        probed = list(await asyncio.gather(*tasks, return_exceptions=True))
        result = list(unresolved)
        for item in probed:
            if isinstance(item, RuntimeProbeResult):
                result.append(item)
            else:
                result.append(
                    RuntimeProbeResult(
                        runtimeId="probe-error",
                        displayName="Probe error",
                        release="experimental",
                        available=False,
                        enabled=False,
                        diagnostics=[str(item)[:1000]],
                        trustState="rejected",
                        compatibilityState="error",
                    )
                )
        return sorted(result, key=lambda item: item.runtime_id)


runtime_registry = HarnessRuntimeRegistry()


__all__ = ["HarnessRuntimeRegistry", "runtime_registry"]
