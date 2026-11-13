"""Compatibility for agent names received at top-level chat entry points."""


def normalize_primary_agent_name(agent_name: str | None) -> str:
    """Map the old AG-UI default to build without changing the general subagent.

    Historical AG-UI threads used ``general`` and the Web composer could carry
    that value into new chats. Apply this alias only when selecting a primary
    agent, never when creating or displaying a child session.
    """
    name = str(agent_name or "").strip()
    return "build" if name == "general" else name
