"""Agent-specific MCP definition collections.

The collection is model-facing and deliberately does not publish MCP tools to
the process-global ToolRegistry. Execution resolves MCP tools from MCPManager,
which preserves the dedicated MCP route and avoids cross-agent registrations.
"""

import os
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from .manager_auto import MCPManager
from .agent_integration import MCPTool, MCPToolIntegrator
from ..types import AgentInfo


@dataclass
class AgentMCPCollection:
    """Collection of MCP tools for a specific agent"""
    agent_name: str
    tools: List[MCPTool]
    enabled_tools: List[str]  # 列表中包含的 MCP 工具名称，可以获取的工具名称列表


class AgentSpecificMCPManager:
    """Manages MCP tools specific to different agents"""
    
    def __init__(self):
        self._agent_tool_collections: Dict[str, AgentMCPCollection] = {}
        self._default_tools: Optional[AgentMCPCollection] = None
        
    async def initialize(self):
        """Initialize the manager and load all MCP tools"""
        await MCPManager.initialize()
        
    async def setup_agent_mcp_tools(self, agent_info: AgentInfo) -> List[MCPTool]:
        """
        Setup MCP tools for a specific agent based on its name/type
        """
        # Get all available MCP tools
        all_mcp_tools = await MCPToolIntegrator.get_available_tools()
        
        # Determine which tools to enable for this agent based on agent type
        filtered_tools = self._filter_tools_by_agent(agent_info, all_mcp_tools)
        
        # Create collection for this agent
        collection = AgentMCPCollection(
            agent_name=agent_info.name,
            tools=filtered_tools,
            enabled_tools=[tool._tool_name for tool in filtered_tools]
        )
        
        self._agent_tool_collections[agent_info.name] = collection
        
        # MCPManager remains the execution catalog. Publishing these adapters
        # into the global ToolRegistry would make the next model iteration see
        # each MCP definition twice and would route execution as a builtin.
        return filtered_tools
    
    def _filter_tools_by_agent(self, agent_info: AgentInfo, all_tools: List[MCPTool]) -> List[MCPTool]:
        """
        Filter MCP tools based on agent type
        """
        # Routed compute services belong to the executor-side catalog.  They
        # must not appear as direct model tools even when a broad mcp.json is
        # loaded accidentally; the model submits them through job-protocol.
        all_tools = [
            tool
            for tool in all_tools
            if getattr(tool, "is_model_visible", True)
            and MCPManager.get_server_route_info(
                getattr(tool, "server_name", "")
            ).get("user_callable", False)
        ]

        agent_name = agent_info.name.lower()
        alias_name = agent_name.replace("worker_", "protein_", 1) if agent_name.startswith("worker_") else agent_name

        # 固定 worker 的实际 agent 名通常是 worker_xxx，但 MCP 白名单配置按 protein_xxx 维护。
        # 这里做一次别名回退，避免 worker 进来后完全看不到 MCP 工具。
        configured = self._configured_allowed_tools(agent_name)
        if configured is None and alias_name != agent_name:
            configured = self._configured_allowed_tools(alias_name)
        if configured is not None:
            if configured == "all":
                return all_tools
            return [
                tool
                for tool in all_tools
                if self._tool_matches_allowed(tool, configured)
            ]
        
        # Define tool categories for different agent types
        tool_filters = {
            # Protein-related agents get bio-specific tools
            'protein_requirements': ['ligandmpnn_run_mcp_server', 'boltz_mcp_server', 'gnina_mcp_server'],
            'protein_design': ['ligandmpnn_run_mcp_server', 'boltz_mcp_server', 'placer_mcp_server'],
            'protein_structure': ['boltz_mcp_server', 'plip_mcp_server', 'gnina_mcp_server'],
            'protein_experiment': ['gnina_mcp_server', 'plip_mcp_server', 'rfdiffusion_mcp_server'],
            
            # Orchestration agent gets all tools
            'protein_orchestrator': 'all',
            
            # Route policy has already removed every server that must go through
            # job-protocol. Let the two default agents discover any remaining
            # direct server so an installed third-party MCP App works without a
            # TCode source-code change. A per-agent allowed_tools entry still
            # takes precedence when a deployment wants a narrower catalog.
            'general': 'all',
            'build': 'all'
        }
        
        # Check if agent has specific tool requirements
        if agent_name in tool_filters:
            required_tools = tool_filters[agent_name]
            if required_tools == 'all':
                return all_tools
            else:
                return [
                    tool
                    for tool in all_tools
                    if self._tool_matches_allowed(tool, required_tools)
                ]
        if alias_name in tool_filters:
            required_tools = tool_filters[alias_name]
            if required_tools == 'all':
                return all_tools
            return [
                tool
                for tool in all_tools
                if self._tool_matches_allowed(tool, required_tools)
            ]
        else:
            # 固定 worker 如果没有显式配置，优先尝试别名映射；仍然没有时再回退到空集合。
            if agent_name.startswith("worker_"):
                return []
            return all_tools

    def _configured_allowed_tools(self, agent_name: str) -> Optional[list[str] | str]:
        try:
            from ..agent.agents import BuiltinAgents

            config = BuiltinAgents.get_agent_mcp_config(
                agent_name,
                workdir=os.environ.get("TCODE_WORKDIR") or os.getcwd(),
            )
        except Exception:
            config = None
        if not isinstance(config, dict) or "allowed_tools" not in config:
            return None
        raw_allowed = config.get("allowed_tools")
        if raw_allowed == "all":
            return "all"
        if not isinstance(raw_allowed, list):
            return []
        allowed = [
            str(item or "").strip()
            for item in raw_allowed
            if str(item or "").strip()
        ]
        return allowed

    @staticmethod
    def _normalize_tool_key(value: str) -> str:
        return (
            str(value or "")
            .strip()
            .lower()
            .replace("_", "-")
            .replace(".", "-")
        )

    @classmethod
    def _tool_matches_allowed(cls, tool: MCPTool, allowed_tools: list[str]) -> bool:
        if not allowed_tools:
            return False
        server_key = cls._normalize_tool_key(getattr(tool, "server_name", ""))
        tool_name_key = cls._normalize_tool_key(getattr(tool, "_tool_name", ""))
        tool_id_key = cls._normalize_tool_key(getattr(tool, "id", ""))
        combined_key = cls._normalize_tool_key(
            f"{getattr(tool, 'server_name', '')}-{getattr(tool, '_tool_name', '')}"
        )
        for raw in allowed_tools:
            allowed = cls._normalize_tool_key(raw)
            if not allowed:
                continue
            if allowed == "all":
                return True
            if allowed in {server_key, tool_name_key, tool_id_key, combined_key}:
                return True
            if allowed.endswith("-mcp") and allowed == server_key:
                return True
            if server_key and allowed in server_key:
                return True
            if tool_id_key and allowed in tool_id_key:
                return True
        return False
    
    def get_agent_tools(self, agent_name: str) -> Optional[AgentMCPCollection]:
        """Get the MCP tools collection for a specific agent"""
        return self._agent_tool_collections.get(agent_name)
    
    def get_all_agent_collections(self) -> Dict[str, AgentMCPCollection]:
        """Get all agent MCP collections"""
        return self._agent_tool_collections.copy()
    
    async def refresh_agent_tools(self, agent_name: str) -> Optional[AgentMCPCollection]:
        """Refresh tools for a specific agent"""
        if agent_name in self._agent_tool_collections:
            agent_collection = self._agent_tool_collections[agent_name]
            # Re-fetch tools from MCP manager
            all_mcp_tools = await MCPToolIntegrator.get_available_tools()
            filtered_tools = self._filter_tools_by_agent(
                AgentInfo(name=agent_name, description="", mode=None), 
                all_mcp_tools
            )
            # Update collection
            agent_collection.tools = filtered_tools
            agent_collection.enabled_tools = [tool._tool_name for tool in filtered_tools]
            return agent_collection
        return None


# Global instance
_AGENT_SPECIFIC_MCP_MANAGER = AgentSpecificMCPManager()


async def get_agent_specific_mcp_manager() -> AgentSpecificMCPManager:
    """Get the global agent-specific MCP manager instance"""
    if not _AGENT_SPECIFIC_MCP_MANAGER._default_tools:
        await _AGENT_SPECIFIC_MCP_MANAGER.initialize()
    return _AGENT_SPECIFIC_MCP_MANAGER
