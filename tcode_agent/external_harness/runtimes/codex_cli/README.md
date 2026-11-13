# Codex CLI Harness Adapter

- Runtime ID: `codex-cli`
- Executable: `codex`
- Transport: Codex JSONL over stdio
- Session: ephemeral in the current release
- Permission authority: the runtime
- Public server: disabled

`adapter.py` is the composition entry point and `definition.py` contains all
Codex-specific declarations. Process control, prompt handoff and JSONL
framing/event normalization remain shared.

TCode platform-plugin projection is not part of this adapter yet.
