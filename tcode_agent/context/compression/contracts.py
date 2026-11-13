"""Host-owned contracts. Token estimates are explicitly distinct from usage."""
from dataclasses import dataclass, replace
import fnmatch
import json
import os


@dataclass(frozen=True)
class Owner:
    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str

    def key(self) -> str:
        values = (self.tenant_id, self.user_id, self.workspace_id, self.session_id)
        if not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError("owner_incomplete")
        return json.dumps(values, ensure_ascii=False)


@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    min_chars: int = 8000
    max_bytes: int = 2_000_000
    max_latency_ms: int = 50
    ttl: int = 604800
    quota_bytes: int = 64_000_000
    max_calls: int = 4
    tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()
    allow_groups: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_env(cls):
        prefix = "TCODE_CONTEXT_"
        def integer(name, default, low, high):
            try:
                return max(low, min(high, int(os.environ.get(prefix + name, default))))
            except (TypeError, ValueError):
                return default
        mode = os.environ.get(prefix + "COMPRESSION_MODE", "off")
        if mode not in {"off", "shadow", "enforce"}:
            mode = "off"
        if os.environ.get(prefix + "COMPRESSION_ENABLED", "1").lower() in {"0", "false", "off"}:
            mode = "off"
        split = lambda key: tuple(x.strip() for x in os.environ.get(prefix + key, "").split(",") if x.strip())
        return cls(mode=mode,
                   min_chars=integer("COMPRESSION_MIN_TOKENS", 2000, 100, 100000) * 4,
                   max_bytes=integer("COMPRESSION_MAX_BYTES", 2_000_000, 1000, 10_000_000),
                   max_latency_ms=integer("COMPRESSION_MAX_LATENCY_MS", 50, 1, 1000),
                   ttl=integer("CAPSULE_TTL_SECONDS", 604800, 60, 2592000),
                   quota_bytes=integer("CAPSULE_QUOTA_BYTES", 64_000_000, 10000, 512_000_000),
                   max_calls=integer("EXPAND_MAX_CALLS_PER_TURN", 4, 1, 20),
                   tools=split("COMPRESSION_TOOLS"), deny_tools=split("COMPRESSION_DENY_TOOLS"))

    def admits(self, tool: str) -> bool:
        # Shadow is observation-only, so an empty host allowlist deliberately
        # means "observe every otherwise-safe tool".  Enforce remains fail
        # closed: it always requires an explicit host allowlist match.
        host_match = any(fnmatch.fnmatchcase(tool, p) for p in self.tools)
        host_allowed = (self.mode == "shadow" and not self.tools) or host_match
        return (self.mode != "off" and tool != "context_expand"
                and host_allowed
                and all(any(fnmatch.fnmatchcase(tool, p) for p in group) for group in self.allow_groups)
                and not any(fnmatch.fnmatchcase(tool, p) for p in self.deny_tools))

    def restricted(self, *settings):
        """Session/workspace and agent settings may narrow host admission only."""
        policy = self
        ranks = {"off": 0, "shadow": 1, "enforce": 2}
        for setting in settings:
            if not isinstance(setting, dict):
                continue
            mode = setting.get("mode", policy.mode)
            mode = mode if mode in ranks else "off"
            if ranks[mode] < ranks[policy.mode]:
                policy = replace(policy, mode=mode)
            for field in ("tools", "deny_tools"):
                values = setting.get(field)
                if isinstance(values, list) and all(isinstance(v, str) for v in values):
                    if field == "tools":
                        policy = replace(policy, allow_groups=(*policy.allow_groups, tuple(values)))
                    else:
                        policy = replace(policy, deny_tools=(*policy.deny_tools, *values))
        return policy
