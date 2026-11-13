"""Built-in external Harness runtime packages.

Each runtime owns its invocation and capability declaration in a dedicated
package.  Protocol drivers and lifecycle primitives remain shared so adding a
runtime does not copy ACP/JSONL implementations.
"""

from __future__ import annotations

from ..runtime_def import HarnessRuntimeDef
from .claude_code import runtime_definition as claude_code_runtime_definition
from .codex_cli import runtime_definition as codex_cli_runtime_definition
from .opencode import runtime_definition as opencode_runtime_definition


def builtin_runtime_defs() -> list[HarnessRuntimeDef]:
    """Return built-in runtimes in stable product order."""

    return [
        opencode_runtime_definition(),
        codex_cli_runtime_definition(),
        claude_code_runtime_definition(),
    ]


def builtin_adapter_types():
    """Return concrete adapter entry points without creating import cycles.

    Runtime definitions are needed while :mod:`external_harness.registry` is
    importing.  Concrete adapters depend on that registry, so they are loaded
    lazily only when the backend registry is assembled.
    """

    from .claude_code.adapter import ClaudeCodeHarnessAdapter
    from .codex_cli.adapter import CodexCliHarnessAdapter
    from .opencode.adapter import OpenCodeHarnessAdapter

    return [
        OpenCodeHarnessAdapter,
        CodexCliHarnessAdapter,
        ClaudeCodeHarnessAdapter,
    ]


__all__ = [
    "builtin_runtime_defs",
    "builtin_adapter_types",
    "claude_code_runtime_definition",
    "codex_cli_runtime_definition",
    "opencode_runtime_definition",
]
