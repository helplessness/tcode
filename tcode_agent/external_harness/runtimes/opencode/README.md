# OpenCode Harness Adapter

- Runtime ID: `opencode`
- Executable: `opencode`
- Invocation: `opencode acp`; the shared driver sets process/session cwd.
- Transport: ACP v1 over stdio
- Session: ACP `session/load`, falling back to `session/new`
- Permission authority: shared between OpenCode and TCode
- Guidance: cancel the current turn and restart in the same OpenCode session
- Public server: allowed only after the runtime is explicitly enabled

`adapter.py` is the composition entry point. `definition.py` owns every
OpenCode-specific launch, probe, capability and security declaration. ACP
framing, process termination, session persistence and handoff delivery are
shared implementations under `external_harness/`.

CLI convenience flags are not assumed across releases: no `--cwd` or `--pure`
is needed for the shared ACP launch contract. Protocol capabilities are
negotiated through `initialize`, not inferred from a CLI version number.
Run-scoped platform tokens, async restoration and compute replay guards remain
server-owned; dropping a CLI flag does not replace those checks or enable an
automatic retry/fallback to a different runtime.

TCode persists the selected runtime, model, switch version and source run with
each guidance item. On direct guidance it cancels the active OpenCode run and
starts the continuation through this adapter. The driver first tries
`session/load`; if that fails, it creates a new ACP session and injects bounded
canonical conversation context before the latest guidance.

TCode platform-plugin projection is not part of this adapter yet. Do not infer
plugin compatibility from the runtime's general tool or MCP capabilities.
