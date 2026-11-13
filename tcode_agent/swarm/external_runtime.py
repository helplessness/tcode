"""External swarm runtime with decentralized message routing.

This module provides a real swarm-like runtime architecture:
- Agent Pool: manages all agent instances
- Environment: shared virtual space for task, pheromone map, constraints
- Message Bus: indirect communication middleware (memory/redis/kafka facade)
- Monitor & Logger: tracks convergence and performance
- Task Orchestrator: injects initial task only, no centralized scheduling decisions
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Callable, Awaitable

from .observability import SwarmTraceStore


class ExternalSwarmRuntimeError(RuntimeError):
    """Raised when external swarm runtime cannot execute."""


@dataclass
class SwarmMessage:
    """Message passed between agents via message bus."""

    sender: str
    target: str
    content: str
    round_index: int
    created_at: float = field(default_factory=time.time)


@dataclass
class SwarmEnvironment:
    """Shared virtual space for swarm state."""

    task: str
    pheromone_map: dict[str, float] = field(default_factory=dict)
    global_constraints: dict[str, str] = field(default_factory=dict)
    shared_memory: dict[str, Any] = field(default_factory=dict)

    def apply_pheromone_updates(self, updates: dict[str, float]):
        if not updates:
            return
        for key, delta in updates.items():
            self.pheromone_map[key] = self.pheromone_map.get(key, 0.0) + float(delta)

    def evaporate(self, ratio: float = 0.03):
        if not self.pheromone_map:
            return
        for key, value in list(self.pheromone_map.items()):
            self.pheromone_map[key] = value * (1.0 - ratio)


class MessageBus:
    """Abstract message bus interface for indirect communication."""

    async def publish(self, message: SwarmMessage):
        raise NotImplementedError

    async def consume(self, target: str) -> list[SwarmMessage]:
        raise NotImplementedError


class InMemoryMessageBus(MessageBus):
    """In-process message bus used by default."""

    def __init__(self):
        self._queue: dict[str, list[SwarmMessage]] = {}

    async def publish(self, message: SwarmMessage):
        self._queue.setdefault(message.target, []).append(message)

    async def consume(self, target: str) -> list[SwarmMessage]:
        messages = list(self._queue.get(target, []))
        self._queue[target] = []
        return messages


class RedisPubSubBus(InMemoryMessageBus):
    """Redis bus facade.

    Uses Redis list queue when available, with in-memory fallback.
    """

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._client = None
        redis_url = os.environ.get("TCODE_SWARM_REDIS_URL", "redis://127.0.0.1:6379/0")
        try:
            import redis  # type: ignore

            self._client = redis.Redis.from_url(redis_url)
            self._client.ping()
            self._enabled = True
        except Exception:
            self._enabled = False
            self._client = None

    async def publish(self, message: SwarmMessage):
        if not self._enabled or self._client is None:
            await super().publish(message)
            return

        payload = {
            "sender": message.sender,
            "target": message.target,
            "content": message.content,
            "round_index": message.round_index,
            "created_at": message.created_at,
        }
        key = f"swarm:inbox:{message.target}"
        await asyncio.to_thread(self._client.rpush, key, json.dumps(payload, ensure_ascii=False))

    async def consume(self, target: str) -> list[SwarmMessage]:
        if not self._enabled or self._client is None:
            return await super().consume(target)

        key = f"swarm:inbox:{target}"

        def _fetch_and_clear() -> list[bytes]:
            pipe = self._client.pipeline()
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            raw, _ = pipe.execute()
            return raw or []

        raw_items = await asyncio.to_thread(_fetch_and_clear)
        messages: list[SwarmMessage] = []
        for raw in raw_items:
            try:
                data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
                messages.append(
                    SwarmMessage(
                        sender=str(data.get("sender", "")),
                        target=str(data.get("target", target)),
                        content=str(data.get("content", "")),
                        round_index=int(data.get("round_index", 0)),
                        created_at=float(data.get("created_at", time.time())),
                    )
                )
            except Exception:
                continue
        return messages


class KafkaBus(InMemoryMessageBus):
    """Kafka bus facade.

    Uses in-memory behavior as compatibility fallback when kafka package/broker
    is unavailable. This keeps runtime operational without hard dependency.
    """


@dataclass
class AgentTurnResult:
    """One agent turn output."""

    answer: str
    outbound_messages: list[SwarmMessage]
    pheromone_updates: dict[str, float]
    converged: bool
    latency_ms: float
    used_fallback: bool = False
    handoff_target: str = ""
    broadcast_count: int = 0


@dataclass
class ExternalSwarmAgent:
    """Swarm agent wrapper with state, behavior rules and comm interface."""

    name: str
    session_id: str
    description: str
    system_prompt: str
    model_name: str
    swarms_agent: Any
    executor: Optional[
        Callable[[str, str, str, int], Awaitable[dict[str, Any]]]
    ] = None
    state: dict[str, Any] = field(default_factory=dict)
    behavior_rules: list[str] = field(default_factory=list)

    def _extract_tagged_block(self, text: str, tag: str) -> str:
        pattern = rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>"
        match = re.search(pattern, str(text or ""), flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return match.group(1).strip()

    def _parse_outbound_messages(
        self,
        text: str,
        candidates: list[str],
        round_index: int,
    ) -> list[SwarmMessage]:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        results: list[SwarmMessage] = []
        allowed = set(candidates)

        for line in lines:
            match = re.match(
                r"^TO\s*:\s*([a-zA-Z0-9_\-]+)\s+MESSAGE\s*:\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            target = match.group(1).strip()
            content = match.group(2).strip()
            if not content:
                continue

            if target.lower() == "all":
                for candidate in candidates:
                    if candidate == self.name:
                        continue
                    results.append(
                        SwarmMessage(
                            sender=self.name,
                            target=candidate,
                            content=content,
                            round_index=round_index,
                        )
                    )
                continue

            if target in allowed and target != self.name:
                results.append(
                    SwarmMessage(
                        sender=self.name,
                        target=target,
                        content=content,
                        round_index=round_index,
                    )
                )

        return results

    def _parse_broadcast_message(
        self,
        text: str,
        candidates: list[str],
        round_index: int,
    ) -> list[SwarmMessage]:
        payload = str(text or "").strip()
        if not payload:
            return []
        return [
            SwarmMessage(
                sender=self.name,
                target=candidate,
                content=payload,
                round_index=round_index,
            )
            for candidate in candidates
            if candidate != self.name
        ]

    def _parse_handoff_target(self, text: str, candidates: list[str]) -> str:
        content = str(text or "")
        # Strict handoff format: TO:<agent_name> PAYLOAD:<summary>
        match = re.search(
            r"TO\s*:\s*([a-zA-Z0-9_\-]+)\s+PAYLOAD\s*:\s*(.+)",
            content,
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        target = match.group(1).strip()
        if target in candidates and target != self.name:
            return target
        return ""

    def _parse_pheromone_updates(self, text: str) -> dict[str, float]:
        updates: dict[str, float] = {}
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        for line in lines:
            # Format: key=value where value is numeric delta.
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            try:
                delta = float(value.strip())
            except Exception:
                continue
            if key:
                updates[key] = updates.get(key, 0.0) + delta
        return updates

    async def run_turn(
        self,
        *,
        env: SwarmEnvironment,
        inbox: list[SwarmMessage],
        candidates: list[str],
        round_index: int,
    ) -> AgentTurnResult:
        communication_mode = str(
            env.global_constraints.get("communication_mode")
            or env.shared_memory.get("communication_mode")
            or "hybrid"
        ).strip().lower()
        if communication_mode == "all":
            # Backward compatibility for old clients/configs.
            communication_mode = "hybrid"
        pheromone_top = sorted(
            env.pheromone_map.items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:5]

        inbox_text = "\n\n".join(
            [f"FROM:{msg.sender}\nMESSAGE:{msg.content}" for msg in inbox]
        )
        rules = "\n".join([f"- {rule}" for rule in self.behavior_rules])

        prompt = (
            f"Global task:\n{env.task}\n\n"
            f"Round: {round_index}\n"
            f"Global constraints:\n{json.dumps(env.global_constraints, ensure_ascii=False)}\n\n"
            f"Pheromone map top-k:\n{json.dumps(pheromone_top, ensure_ascii=False)}\n\n"
            f"Your previous answer:\n{self.state.get('last_answer', '')}\n\n"
            f"Messages in your inbox (strict routed):\n{inbox_text or '(empty)'}\n\n"
            f"Communication mode: {communication_mode}\n"
            "Behavior rules:\n"
            f"{rules or '- Keep concise and actionable'}\n\n"
            "Execution requirement:\n"
            "- Do not reply with only suggestions or shell commands.\n"
            "- If task asks for concrete facts (counts, files, versions, status), execute available tools first, then report measured results.\n"
            "- <updated_answer> must contain the final user-facing answer, not a plan.\n\n"
            "Output strictly in this format:\n"
            "<peer_messages>\n"
            "TO:<agent_name> MESSAGE:<one concise message>\n"
            "</peer_messages>\n"
            "<broadcast_message>one concise message for all peers (optional)</broadcast_message>\n"
            "<handoff>TO:<agent_name> PAYLOAD:<what should be continued></handoff>\n"
            "<pheromone_updates>\n"
            "topic_a=+0.2\n"
            "topic_b=-0.1\n"
            "</pheromone_updates>\n"
            "<updated_answer>Your improved answer</updated_answer>\n"
            "<converged>true|false</converged>"
        )

        started = time.perf_counter()

        if self.executor is not None:
            exec_result = await self.executor(self.name, self.session_id, prompt, round_index)
            raw = str(exec_result.get("content", "") or "")
        else:
            if self.swarms_agent is None:
                raise ExternalSwarmRuntimeError(
                    f"No executor or swarms agent available for subagent {self.name}"
                )

            def _run() -> str:
                result = self.swarms_agent.run(prompt)
                return "" if result is None else str(result)

            raw = await asyncio.to_thread(_run)
        latency_ms = (time.perf_counter() - started) * 1000.0

        peer_messages_block = self._extract_tagged_block(raw, "peer_messages")
        broadcast_block = self._extract_tagged_block(raw, "broadcast_message")
        handoff_block = self._extract_tagged_block(raw, "handoff")
        pheromone_block = self._extract_tagged_block(raw, "pheromone_updates")
        updated_answer = self._extract_tagged_block(raw, "updated_answer")
        converged_text = self._extract_tagged_block(raw, "converged").lower()

        outbound: list[SwarmMessage] = []
        if communication_mode in {"p2p", "hybrid"}:
            outbound.extend(self._parse_outbound_messages(peer_messages_block, candidates, round_index))
        if communication_mode in {"broadcast", "hybrid"}:
            outbound.extend(self._parse_broadcast_message(broadcast_block, candidates, round_index))

        handoff_target = ""
        if communication_mode in {"handoff", "hybrid"}:
            handoff_target = self._parse_handoff_target(handoff_block, candidates)
            if handoff_target:
                outbound.append(
                    SwarmMessage(
                        sender=self.name,
                        target=handoff_target,
                        content=f"HANDOFF from {self.name}: continue this focus with your specialty.",
                        round_index=round_index,
                    )
                )

        pheromone_updates = self._parse_pheromone_updates(pheromone_block)
        answer = updated_answer or raw
        converged = converged_text in {"1", "true", "yes", "y"}

        self.state["last_answer"] = answer
        self.state["last_latency_ms"] = latency_ms
        self.state["last_outbound"] = len(outbound)

        return AgentTurnResult(
            answer=answer,
            outbound_messages=outbound,
            pheromone_updates=pheromone_updates,
            converged=converged,
            latency_ms=latency_ms,
            used_fallback=False,
            handoff_target=handoff_target,
            broadcast_count=(len(self._parse_broadcast_message(broadcast_block, candidates, round_index)) if broadcast_block else 0),
        )


@dataclass
class AgentPool:
    """Manages all swarm agent instances."""

    agents: dict[str, ExternalSwarmAgent]

    @classmethod
    def build(
        cls,
        *,
        swarms_agent_cls: Optional[Any],
        agent_specs: list[dict[str, str]],
        model_name: str,
        executor: Optional[Callable[[str, str, str, int], Awaitable[dict[str, Any]]]] = None,
    ) -> "AgentPool":
        built: dict[str, ExternalSwarmAgent] = {}
        for spec in agent_specs:
            name = spec["name"]
            swarms_agent = None
            if swarms_agent_cls is not None:
                swarms_agent = swarms_agent_cls(
                    agent_name=name,
                    agent_description=spec.get("description", f"{name} specialist"),
                    system_prompt=spec.get("system_prompt", f"You are {name}."),
                    model_name=model_name,
                    max_loops=1,
                )

            built[name] = ExternalSwarmAgent(
                name=name,
                session_id=spec["session_id"],
                description=spec.get("description", f"{name} specialist"),
                system_prompt=spec.get("system_prompt", f"You are {name}."),
                model_name=model_name,
                swarms_agent=swarms_agent,
                executor=executor,
                behavior_rules=[
                    "Only send strict TO:<agent> MESSAGE:<text> messages",
                    "Respect global constraints and minimize noise",
                    "Improve answer quality each round",
                ],
            )
        return cls(agents=built)

    def names(self) -> list[str]:
        return list(self.agents.keys())

    def items(self):
        return self.agents.items()


@dataclass
class SwarmMonitor:
    """Tracks swarm status, convergence, and performance logs."""

    run_id: str
    started_at: float = field(default_factory=time.time)
    round_logs: list[dict[str, Any]] = field(default_factory=list)
    agent_turns: dict[str, int] = field(default_factory=dict)
    agent_busy_turns: dict[str, int] = field(default_factory=dict)

    def record_round(
        self,
        round_index: int,
        *,
        outbound_count: int,
        converged_count: int,
        avg_latency_ms: float,
        pheromone_keys: int,
        pheromone_gini: float,
        fallback_count: int,
        handoff_count: int,
        busy_agents: int,
        total_agents: int,
    ):
        self.round_logs.append(
            {
                "round": round_index,
                "outbound_messages": outbound_count,
                "converged_agents": converged_count,
                "avg_latency_ms": avg_latency_ms,
                "pheromone_keys": pheromone_keys,
                "pheromone_gini": pheromone_gini,
                "fallback_count": fallback_count,
                "handoff_count": handoff_count,
                "busy_agents": busy_agents,
                "agent_utilization": (busy_agents / max(1, total_agents)),
                "timestamp": time.time(),
            }
        )

    def mark_agent_turn(self, agent_name: str, *, busy: bool):
        self.agent_turns[agent_name] = self.agent_turns.get(agent_name, 0) + 1
        if busy:
            self.agent_busy_turns[agent_name] = self.agent_busy_turns.get(agent_name, 0) + 1

    def utilization(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for agent_name, turns in self.agent_turns.items():
            result[agent_name] = self.agent_busy_turns.get(agent_name, 0) / max(1, turns)
        return result

    def is_converged(self, agent_count: int) -> bool:
        if not self.round_logs:
            return False
        last = self.round_logs[-1]
        # Only stop when all agents explicitly report convergence.
        # Zero outbound messages alone can happen in early turns and may cause
        # premature stop before final concrete results are produced.
        return int(last.get("converged_agents", 0)) >= agent_count

    def dump(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self.run_id}_swarm_monitor.json"
        payload = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": time.time(),
            "round_logs": self.round_logs,
            "agent_utilization": self.utilization(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ExternalSwarmRuntime:
    """Decentralized external swarm runtime orchestrator."""

    def __init__(
        self,
        *,
        run_id: str,
        model_name: str,
        topology: str,
        max_rounds: int,
        bus_backend: str = "memory",
        agent_timeout_seconds: float = 90.0,
        max_retries: int = 2,
        fallback_on_error: bool = True,
        execute_agent_turn: Optional[
            Callable[[str, str, str, int], Awaitable[dict[str, Any]]]
        ] = None,
        session_id: str = "",
    ):
        self.run_id = run_id
        self.model_name = model_name
        self.topology = topology
        self.max_rounds = max(1, min(8, int(max_rounds)))
        self.agent_timeout_seconds = max(5.0, float(agent_timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self.fallback_on_error = bool(fallback_on_error)
        self.execute_agent_turn = execute_agent_turn
        self.session_id = str(session_id or "").strip()
        self.bus_backend = str(bus_backend or "memory").strip().lower() or "memory"
        self.monitor = SwarmMonitor(run_id=run_id)
        self.message_bus = self._create_bus(bus_backend)

    async def _run_agent_text(
        self,
        *,
        agent: ExternalSwarmAgent,
        prompt: str,
        round_index: int,
    ) -> str:
        if agent.executor is not None:
            result = await agent.executor(agent.name, agent.session_id, prompt, round_index)
            return str(result.get("content", "") or "")

        if agent.swarms_agent is None:
            raise ExternalSwarmRuntimeError(
                f"No executor or swarms agent available for subagent {agent.name}"
            )

        def _run() -> str:
            output = agent.swarms_agent.run(prompt)
            return "" if output is None else str(output)

        return await asyncio.to_thread(_run)

    def _extract_vote_target(self, text: str, candidates: list[str]) -> str:
        content = str(text or "")
        match = re.search(r"VOTE\s*[:：]\s*([a-zA-Z0-9_\-]+)", content)
        if match:
            voted = match.group(1).strip()
            if voted in candidates:
                return voted

        lowered = content.lower()
        for candidate in candidates:
            if re.search(rf"\b{re.escape(candidate.lower())}\b", lowered):
                return candidate
        return ""

    def _extract_rearrange_order(self, text: str, candidates: list[str]) -> list[str]:
        content = str(text or "")
        match = re.search(r"ORDER\s*[:：]\s*([a-zA-Z0-9_,\-\s]+)", content)
        if not match:
            return list(candidates)

        ordered: list[str] = []
        seen: set[str] = set()
        for part in match.group(1).split(","):
            name = part.strip()
            if name in candidates and name not in seen:
                ordered.append(name)
                seen.add(name)

        for candidate in candidates:
            if candidate not in seen:
                ordered.append(candidate)
        return ordered if ordered else list(candidates)

    async def _run_voting_phase(
        self,
        *,
        pool: AgentPool,
        task: str,
        outputs: dict[str, str],
        round_index: int,
    ) -> dict[str, Any]:
        candidates = list(outputs.keys())
        if len(candidates) <= 1:
            return {
                "winner": candidates[0] if candidates else "",
                "tally": {name: 1 for name in candidates},
            }

        candidate_text = "\n\n".join(
            [f"[{name}]\n{outputs.get(name, '')}" for name in candidates]
        )
        votes: dict[str, int] = {name: 0 for name in candidates}

        for name in candidates:
            agent = pool.agents[name]
            vote_prompt = (
                f"Original task:\n{task}\n\n"
                "You are in voting topology. Choose the best candidate answer among peers.\n"
                "Reply with first line exactly: VOTE:<agent_name>\n"
                "Then briefly explain why.\n\n"
                f"Candidates:\n{candidate_text}"
            )
            vote_output = await self._run_agent_text(
                agent=agent,
                prompt=vote_prompt,
                round_index=round_index,
            )
            voted = self._extract_vote_target(vote_output, candidates)
            if voted:
                votes[voted] = votes.get(voted, 0) + 1

        winner = max(candidates, key=lambda candidate: votes.get(candidate, 0))
        winner_output = outputs.get(winner, "")
        tally_lines = "\n".join([f"- {name}: {votes.get(name, 0)}" for name in candidates])
        merged = (
            f"Voting winner: {winner}\n\n"
            f"Vote tally:\n{tally_lines}\n\n"
            f"Winner output:\n{winner_output}"
        )

        for name in candidates:
            outputs[name] = merged

        return {
            "winner": winner,
            "tally": votes,
        }

    async def _run_rearrange_phase(
        self,
        *,
        pool: AgentPool,
        task: str,
        outputs: dict[str, str],
        round_index: int,
    ) -> dict[str, Any]:
        candidates = list(outputs.keys())
        if len(candidates) <= 1:
            return {"order": candidates}

        planner_name = candidates[0]
        planner = pool.agents[planner_name]
        candidate_text = "\n\n".join(
            [f"[{name}]\n{outputs.get(name, '')}" for name in candidates]
        )
        order_prompt = (
            f"Original task:\n{task}\n\n"
            "You are arranging execution order for rearrange topology.\n"
            "Reply first line exactly as ORDER:a,b,c using only candidate names.\n"
            f"Candidates: {', '.join(candidates)}\n\n"
            f"Current outputs:\n{candidate_text}"
        )
        order_output = await self._run_agent_text(
            agent=planner,
            prompt=order_prompt,
            round_index=round_index,
        )
        ordered_names = self._extract_rearrange_order(order_output, candidates)

        refined_outputs: dict[str, str] = {}
        for name in ordered_names:
            agent = pool.agents[name]
            prior_context = "\n\n".join(
                [f"[{agent_name}]\n{value}" for agent_name, value in refined_outputs.items()]
            )
            rearrange_prompt = (
                f"Original task:\n{task}\n\n"
                f"Your previous output:\n{outputs.get(name, '')}\n\n"
                + (
                    f"Prioritized peer outputs (rearranged order):\n{prior_context}\n\n"
                    if prior_context
                    else ""
                )
                + "Refine your answer considering the current rearranged execution order."
            )
            refined = await self._run_agent_text(
                agent=agent,
                prompt=rearrange_prompt,
                round_index=round_index,
            )
            refined_outputs[name] = refined

        for name in candidates:
            if name in refined_outputs:
                outputs[name] = (
                    f"Rearrange order: {', '.join(ordered_names)}\n\n"
                    f"Refined output ({name}):\n{refined_outputs[name]}"
                )

        return {
            "order": ordered_names,
        }

    def _pheromone_gini(self, pheromone_map: dict[str, float]) -> float:
        values = [max(0.0, float(v)) for v in pheromone_map.values()]
        if not values:
            return 0.0
        n = len(values)
        total = sum(values)
        if total <= 0:
            return 0.0
        sorted_values = sorted(values)
        weighted_sum = 0.0
        for idx, value in enumerate(sorted_values, start=1):
            weighted_sum += idx * value
        gini = (2.0 * weighted_sum) / (n * total) - (n + 1.0) / n
        return max(0.0, min(1.0, gini))

    def _create_bus(self, backend: str) -> MessageBus:
        normalized = str(backend or "memory").strip().lower()
        if normalized == "redis":
            return RedisPubSubBus()
        if normalized == "kafka":
            return KafkaBus()
        return InMemoryMessageBus()

    async def run(
        self,
        *,
        task: str,
        description: str,
        agent_specs: list[dict[str, str]],
        workdir: str,
        constraints: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        swarms_agent_cls: Optional[Any] = None
        if self.execute_agent_turn is None:
            try:
                swarms_module = __import__("swarms", fromlist=["Agent"])
                swarms_agent_cls = getattr(swarms_module, "Agent")
            except Exception as exc:
                raise ExternalSwarmRuntimeError(f"failed to import swarms.Agent: {exc}") from exc

        pool = AgentPool.build(
            swarms_agent_cls=swarms_agent_cls,
            agent_specs=agent_specs,
            model_name=self.model_name,
            executor=self.execute_agent_turn,
        )

        env = SwarmEnvironment(
            task=task,
            global_constraints=(constraints or {}),
            shared_memory={
                "description": description,
                "topology": self.topology,
            },
        )

        names = pool.names()
        outputs: dict[str, str] = {name: "" for name in names}
        SwarmTraceStore.start(
            run_id=self.run_id,
            session_id=self.session_id,
            workdir=workdir,
            topology=self.topology,
            bus_backend=self.bus_backend,
            agent_names=names,
            max_rounds=self.max_rounds,
        )

        # Initial broadcast from task orchestrator: inject task once then exit.
        for target in names:
            await self.message_bus.publish(
                SwarmMessage(
                    sender="task_orchestrator",
                    target=target,
                    content=task,
                    round_index=0,
                )
            )

        for round_index in range(1, self.max_rounds + 1):
            turn_inputs: dict[str, list[SwarmMessage]] = {}
            for agent_name in names:
                turn_inputs[agent_name] = await self.message_bus.consume(agent_name)

            if self.topology == "sequential":
                ordered_names = names
            else:
                ordered_names = names

            async def _run_one(agent_name: str) -> tuple[str, AgentTurnResult]:
                agent = pool.agents[agent_name]

                last_exc: Optional[Exception] = None
                attempts = self.max_retries + 1
                for _ in range(attempts):
                    try:
                        result = await asyncio.wait_for(
                            agent.run_turn(
                                env=env,
                                inbox=turn_inputs.get(agent_name, []),
                                candidates=names,
                                round_index=round_index,
                            ),
                            timeout=self.agent_timeout_seconds,
                        )
                        return (agent_name, result)
                    except Exception as exc:
                        last_exc = exc
                        continue

                if not self.fallback_on_error:
                    raise ExternalSwarmRuntimeError(
                        f"agent {agent_name} failed after retries: {last_exc}"
                    )

                fallback = AgentTurnResult(
                    answer=(
                        f"[fallback] {agent_name} failed this round. "
                        f"Reason: {last_exc}. Keep previous answer and continue swarm."
                    ),
                    outbound_messages=[],
                    pheromone_updates={},
                    converged=False,
                    latency_ms=0.0,
                    used_fallback=True,
                )
                return (agent_name, fallback)

            if self.topology == "sequential":
                turn_results: list[tuple[str, AgentTurnResult]] = []
                for agent_name in ordered_names:
                    turn_results.append(await _run_one(agent_name))
            else:
                turn_results = await asyncio.gather(*[_run_one(agent_name) for agent_name in ordered_names])

            outbound_count = 0
            converged_count = 0
            latency_sum = 0.0
            fallback_count = 0
            handoff_count = 0
            busy_agents = 0
            agent_updates: list[dict[str, Any]] = []
            round_messages: list[dict[str, Any]] = []

            for agent_name, result in turn_results:
                outputs[agent_name] = result.answer
                env.apply_pheromone_updates(result.pheromone_updates)
                outbound_count += len(result.outbound_messages)
                converged_count += 1 if result.converged else 0
                latency_sum += float(result.latency_ms)
                fallback_count += 1 if result.used_fallback else 0
                handoff_count += 1 if result.handoff_target else 0

                is_busy = bool((turn_inputs.get(agent_name) or []) or result.outbound_messages)
                busy_agents += 1 if is_busy else 0
                self.monitor.mark_agent_turn(agent_name, busy=is_busy)
                agent_updates.append(
                    {
                        "agent": agent_name,
                        "inbox_messages": len(turn_inputs.get(agent_name) or []),
                        "outbound_messages": len(result.outbound_messages),
                        "converged": result.converged,
                        "latency_ms": result.latency_ms,
                        "used_fallback": result.used_fallback,
                        "handoff_target": result.handoff_target,
                    }
                )

                for message in result.outbound_messages:
                    await self.message_bus.publish(message)
                    round_messages.append(
                        {
                            "sender": message.sender,
                            "target": message.target,
                            "round": message.round_index,
                            "content": str(message.content or "")[:1200],
                            "created_at": message.created_at,
                        }
                    )

            # Stigmergy: pheromone evaporation keeps exploration/exploitation balanced.
            env.evaporate(ratio=0.08)

            avg_latency = latency_sum / max(1, len(turn_results))
            self.monitor.record_round(
                round_index,
                outbound_count=outbound_count,
                converged_count=converged_count,
                avg_latency_ms=avg_latency,
                pheromone_keys=len(env.pheromone_map),
                pheromone_gini=self._pheromone_gini(env.pheromone_map),
                fallback_count=fallback_count,
                handoff_count=handoff_count,
                busy_agents=busy_agents,
                total_agents=len(names),
            )
            SwarmTraceStore.record_round(
                self.run_id,
                round_data=dict(self.monitor.round_logs[-1]),
                agent_updates=agent_updates,
                messages=round_messages,
                workdir=workdir,
            )

            if self.monitor.is_converged(agent_count=len(names)):
                break

        topology_metrics: dict[str, Any] = {}
        if self.topology == "voting":
            topology_metrics = await self._run_voting_phase(
                pool=pool,
                task=task,
                outputs=outputs,
                round_index=self.max_rounds + 1,
            )
        elif self.topology == "rearrange":
            topology_metrics = await self._run_rearrange_phase(
                pool=pool,
                task=task,
                outputs=outputs,
                round_index=self.max_rounds + 1,
            )

        monitor_root = Path(workdir) / "agent_workspace" / "swarms" / "ExternalSwarmRuntime"
        self.monitor.dump(monitor_root)

        result = {
            "outputs": outputs,
            "metrics": {
                "rounds": len(self.monitor.round_logs),
                "logs": self.monitor.round_logs,
                "pheromone_keys": len(env.pheromone_map),
                "pheromone_gini": self._pheromone_gini(env.pheromone_map),
                "bus_backend": type(self.message_bus).__name__,
                "agent_utilization": self.monitor.utilization(),
                "topology": self.topology,
                "topology_metrics": topology_metrics,
            },
            "trace": {
                "run_id": self.run_id,
                "session_id": self.session_id,
            },
        }
        SwarmTraceStore.finish(
            self.run_id,
            status="completed",
            metrics=result["metrics"],
            workdir=workdir,
        )
        return result
