"""Worker permission scoping - least-privilege isolation for Workers"""

from typing import Optional

from ..types import AgentInfo, AgentMode


class WorkerScoper:
    """
    Maps named permission presets to AgentInfo permission dicts.
    Equivalent to Higress AI Gateway's consumer token in HiClaw:
    Workers only get what they need, real danger is prevented at the preset level.
    """

    PRESETS: dict[str, dict] = {
        "readonly": {
            "*": "deny",
            "read": "allow",
            "grep": "allow",
            "glob": "allow",
            "webfetch": "allow",
            "bash": "ask",
            "ask_peer_question": "allow",
            "answer_peer_question": "allow",
        },
        "developer": {
            "*": "allow",
            "task": "deny",       # Workers must not spawn supervisor subagents
            "todo_write": "deny",
            "todo_read": "deny",
            "create_worker": "deny",  # Workers can't create more Workers
            "assign_task": "deny",
            "terminate_worker": "deny",
        },
        "full": {
            "*": "allow",
            "task": "deny",
            "todo_write": "deny",
            "todo_read": "deny",
            "create_worker": "deny",
            "assign_task": "deny",
            "terminate_worker": "deny",
        },
    }

    VALID_PRESETS = frozenset(PRESETS.keys())

    @classmethod
    def get_preset(cls, name: str) -> dict:
        return dict(cls.PRESETS.get(name, cls.PRESETS["developer"]))

    @classmethod
    def _build_mcp_tool_advisory(
        cls,
        agent_name: str,
        *,
        workdir: Optional[str] = None,
    ) -> str:
        normalized_name = str(agent_name or "").strip()
        if not normalized_name:
            return ""

        try:
            from ..agent.agents import BuiltinAgents

            config = BuiltinAgents.get_agent_mcp_config(normalized_name, workdir)
        except Exception:
            config = None

        if not isinstance(config, dict):
            return ""

        raw_allowed_tools = config.get("allowed_tools")
        if raw_allowed_tools == "all":
            return ""
        if not isinstance(raw_allowed_tools, list):
            return ""

        suggested_tools = [
            str(item or "").strip()
            for item in raw_allowed_tools
            if str(item or "").strip()
        ]
        if not suggested_tools:
            return ""

        return (
            "\n\nMCP 工具建议："
            f"\n- 优先考虑这些工具：{', '.join(suggested_tools)}"
            "\n- 这只是上下文建议，不是硬性限制。"
            "\n- 如果其他可用工具更合适，或上述工具当前不可用，可以改用其他工具，并在结果中简要说明原因。"
        )

    @classmethod
    def build_from_config(cls, worker_cfg: dict, workdir: Optional[str] = None) -> AgentInfo:
        """Build an AgentInfo from a worker config dict (from protein_team.json).

        If the config specifies an `agent_name` that matches a builtin agent,
        that agent's prompt/description are reused but mode is forced to WORKER
        and permission is set from the preset.
        """
        from ..agent.agents import BuiltinAgents

        name = str(worker_cfg.get("name", "worker")).strip().lower()
        role = str(worker_cfg.get("role", name)).strip()
        agent_name = str(worker_cfg.get("agent_name", "")).strip()
        preset = str(worker_cfg.get("permission_preset", "developer")).strip()

        base = BuiltinAgents.get(agent_name) if agent_name else None

        # prompt priority: config prompt_override > builtin agent prompt > default
        prompt_override = worker_cfg.get("prompt_override") or None
        final_prompt = (
            str(prompt_override).strip()
            if prompt_override
            else (base.prompt if base else (
                f"你是一名专职的工人 Agent，你的角色是：{role}。\n\n"
                "专注完成 Manager 分配的任务，完成后给出清晰的结果总结。"
            ))
        )

        peer_whitelist = [
            str(item).strip().lower()
            for item in (worker_cfg.get("peer_whitelist") or [])
            if str(item).strip()
        ]
        if peer_whitelist:
            final_prompt = (
                final_prompt
                + "\n\n如团队开启 peer communication，你只能通过正式协作协议联系白名单内的其他 Worker："
                "\n- missing_fact：缺少关键事实或输入"
                "\n- need_review：需要对方专业复核"
                "\n- conflict_check：需要核对冲突结论"
                "\n发起问题时使用 ask_peer_question。"
                "\n收到协作请求时，只能调用 answer_peer_question 回复一次结构化答复。"
                "\n通信会自动抄送给 Manager，并受到白名单、轮次、超时限制。"
                "\n不要把它当作聊天功能，不要发送寒暄或无推进价值的信息。"
                "\n\n发起示例："
                "\nask_peer_question(target_worker='structure', reason='need_review', question='请判断候选A的局部构象是否存在明显折叠风险，并给出下一步建议。')"
                "\n\n答复示例："
                "\nanswer_peer_question(request_id='...', summary='候选A在loop区域存在局部拥挤风险，但整体折叠仍可能成立。', confidence=0.74, next_action='建议先缩小loop突变幅度后再进入评分。', blockers=['缺少更高分辨率结构证据'])"
            )
        final_prompt += cls._build_mcp_tool_advisory(agent_name, workdir=workdir)

        # permission priority: config permission dict > preset
        cfg_permission = worker_cfg.get("permission") or None
        final_permission = (
            dict(cfg_permission)
            if isinstance(cfg_permission, dict)
            else cls.get_preset(preset)
        )

        # steps priority: config steps > builtin agent steps > None
        cfg_steps = worker_cfg.get("steps")
        final_steps = (
            int(cfg_steps)
            if isinstance(cfg_steps, int)
            else (base.steps if base and base.steps else None)
        )

        agent_info = AgentInfo(
            name=f"worker_{name}",
            description=base.description if base else f"Worker Agent: {role}",
            mode=AgentMode.WORKER,
            permission=final_permission,
            options={
                "display_name": f"Worker: {name}",
                "execution_domain": str(
                    worker_cfg.get("execution_domain") or ""
                ).strip(),
                "workspace_strategy": str(
                    worker_cfg.get("workspace_strategy") or ""
                ).strip(),
            },
            prompt=final_prompt,
            steps=final_steps,
        )
        return agent_info

    @classmethod
    def build_worker_agent_info(
        cls,
        name: str,
        role: str,
        preset: str,
        peer_whitelist: Optional[list[str]] = None,
    ) -> AgentInfo:
        peer_targets = [
            str(item).strip().lower()
            for item in (peer_whitelist or [])
            if str(item).strip()
        ]
        peer_prompt = ""
        if peer_targets:
            peer_prompt = (
                "\n\n正式协作协议："
                "\n- 如需其他 Worker 补充事实、复核结论或核对冲突，请优先使用 "
                f"`ask_peer_question(target_worker=..., reason=..., question=...)` 联系以下白名单 Worker：{', '.join(peer_targets)}"
                "\n- 对方只会用 `answer_peer_question` 回复一次结构化答复"
                "\n- 不要把它当自由聊天通道"
            )

        return AgentInfo(
            name=f"worker_{name}",
            description=f"Worker Agent: {role}",
            mode=AgentMode.WORKER,
            permission=cls.get_preset(preset),
            options={"display_name": f"Worker: {name}"},
            prompt=f"""你是一名专职的工人 Agent，你的角色是：{role}。

你的任务由团队 Manager 分配，你需要专注地完成任务。

工作原则：
- 专注于分配的任务范围，不要偏离
- 完成后给出清晰的总结，说明你做了什么、结果如何
- 遇到无法解决的阻碍时，说明原因
- 不要主动创建新的 Agent 或生成新的任务"""
            + peer_prompt,
        )
