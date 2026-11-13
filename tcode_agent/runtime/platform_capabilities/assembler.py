"""Assemble one authoritative external-Harness capability snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from ...config.manager import ConfigManager
from ...vision import configured_model_supports_images, select_vision_model_ref
from ..agent_backend import NormalizedRunInput, RunContext
from .models import CapabilityGrant, PlatformCapabilitySnapshot
from .policy import PlatformCapabilityPolicy
from .staging import stage_allowed_skills


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.environ.get(name, str(default))), maximum))
    except Exception:
        return default


class PlatformCapabilityAssembler:
    def __init__(self, policy: PlatformCapabilityPolicy | None = None) -> None:
        self.policy = policy or PlatformCapabilityPolicy()

    async def assemble(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
        runtime_capabilities: dict[str, Any],
    ) -> PlatformCapabilitySnapshot | None:
        if not self.policy.allows_runtime(context.runtime_id, runtime_capabilities):
            return None

        config = ConfigManager.load(context.workdir)
        model_image_support = configured_model_supports_images(config, run_input.model_id)
        harness_images = runtime_capabilities.get("images") is True
        native_images = bool(model_image_support is True and harness_images)
        vision_ref = select_vision_model_ref(config, exclude=run_input.model_id)

        grants: list[CapabilityGrant] = []
        if native_images:
            grants.append(
                CapabilityGrant(
                    capabilityId="image.native",
                    status="active",
                    operations=["prompt-image"],
                )
            )
        elif self.policy.allows("image.inspect") and vision_ref:
            grants.append(
                CapabilityGrant(
                    capabilityId="image.inspect",
                    status="active",
                    operations=["look_at"],
                    limits={"maxBytes": 10 * 1024 * 1024, "visionModel": vision_ref},
                )
            )
        else:
            grants.append(
                CapabilityGrant(
                    capabilityId="image.inspect",
                    status="degraded",
                    reason="No admitted vision model is configured for text-model fallback.",
                )
            )

        definitions = (
            ("canvas.read", ["read"]),
            ("canvas.write", ["create", "edit"]),
            ("evidence.read", ["search", "get", "graph"]),
            ("evidence.write", ["create_from_source"]),
            ("wiki.read", ["search", "read"]),
            ("wiki.propose", ["stage_proposal"]),
            ("research.read", ["list_projects", "get_project", "list_packs"]),
            ("skill.read", ["list", "read"]),
            ("artifact.publish", ["publish"]),
            ("job.describe", ["describe_target_tool"]),
            ("job.submit", ["submit"]),
        )
        for capability_id, operations in definitions:
            if self.policy.allows(capability_id):
                grants.append(
                    CapabilityGrant(
                        capabilityId=capability_id,
                        status="active",
                        operations=operations,
                    )
                )

        staged_skills, staging_root = await stage_allowed_skills(
            workdir=context.workdir,
            run_id=context.run_id,
            policy=self.policy,
        )
        skill_grant = next(
            (item for item in grants if item.capability_id == "skill.read"), None
        )
        if skill_grant is not None and not staged_skills:
            skill_grant.status = "degraded"
            skill_grant.reason = "No Skills were admitted by the server allowlist for this Run."

        now = time.time()
        ttl = _bounded_int("TCODE_PLATFORM_BRIDGE_TOKEN_TTL_SECONDS", 1800, 30, 7200)
        max_calls = _bounded_int("TCODE_PLATFORM_BRIDGE_MAX_CALLS", 64, 1, 4096)
        identity = {
            "tenant": context.tenant_id,
            "user": context.user_id,
            "workspace": context.workspace_id,
            "session": context.session_id,
            "run": context.run_id,
            "runtime": context.runtime_id,
            "model": run_input.model_id,
            "capabilities": [item.model_dump(mode="json", by_alias=True) for item in grants],
            "skills": [item.model_dump(mode="json", by_alias=True) for item in staged_skills],
        }
        digest = "sha256:" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return PlatformCapabilitySnapshot(
            snapshotId="pcap_" + uuid.uuid4().hex,
            tenantId=context.tenant_id,
            userId=context.user_id,
            workspaceId=context.workspace_id,
            sessionId=context.session_id,
            runId=context.run_id,
            runtimeId=context.runtime_id,
            modelId=run_input.model_id,
            nativeImageInput=native_images,
            capabilities=grants,
            stagedSkills=staged_skills,
            stagingRoot=staging_root,
            issuedAt=PlatformCapabilitySnapshot.timestamp(now),
            expiresAt=PlatformCapabilitySnapshot.timestamp(now + ttl),
            maxCalls=max_calls,
            auditDigest=digest,
        )


__all__ = ["PlatformCapabilityAssembler"]
