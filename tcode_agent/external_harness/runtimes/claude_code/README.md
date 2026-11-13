# Claude Code Harness Adapter

- Runtime ID: `claude-code`
- Executable: `claude`
- Transport: Claude stream-json over stdio
- Session: ephemeral in the current release
- Permission authority: the runtime
- Public server: disabled

`adapter.py` is the composition entry point and `definition.py` contains all
Claude-specific declarations. Process control, prompt handoff and JSONL event
normalization remain shared.

TCode platform-plugin projection is not part of this adapter yet.
