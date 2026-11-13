"""Terminal adapter for the agent's async Question callback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import sys
from typing import Any, Iterator, TextIO


class TerminalQuestionHandler:
    def __init__(self, stdin: TextIO, output: TextIO) -> None:
        self.stdin = stdin
        self.output = output
        # Capture this before --result-only redirects the console descriptors.
        self.interactive = stdin.isatty() and output.isatty()
        self._lock = asyncio.Lock()

    def _print(self, text: str = "", *, end: str = "\n") -> None:
        print(text, file=self.output, end=end, flush=True)

    async def __call__(self, payload: dict[str, Any]) -> str:
        # Serialize concurrent question requests; only one question owns stdin.
        async with self._lock:
            self._display(payload)
            if not self.interactive:
                self._print(
                    "当前 stdin 或 stderr 不是交互终端，未读取答案。"
                    "请在终端使用 --prompt 启动，或恢复会话补充回答；不会自动确认。"
                )
                return ""

            timeout = max(1, int(payload.get("timeout_seconds") or 300))
            outcome = "保留为待输入" if payload.get("required", True) else "跳过此问题"
            self._print(f"输入 /cancel 暂不回答；{timeout} 秒内未回答将{outcome}。")
            try:
                # One deadline covers retries, not time spent queued for stdin.
                action = self._ask_batch(payload) if payload.get("type") == "batch" else self._ask(payload)
                return await asyncio.wait_for(action, timeout=timeout)
            except TimeoutError:
                self._print("\n回答超时，未提交答案，也不会自动使用默认答案。")
                return ""
            except (EOFError, OSError, ValueError) as exc:
                self._print(f"\n终端输入已结束或不可用，未提交答案。{str(exc)}")
                return ""

    def _display(self, payload: dict[str, Any]) -> None:
        self._print("\n" + "=" * 60)
        self._print("Question · 需要你的回答")
        if payload.get("goal"):
            self._print(f"目标：{payload['goal']}")
        self._print(str(payload.get("question") or payload.get("display") or "请补充信息"))
        if payload.get("type") == "batch":
            self._print(f"共 {len(payload.get('questions') or [])} 个问题，完成后统一提交。")
            self._print("=" * 60)
            return
        options = payload.get("options") or []
        kind = payload.get("type", "custom")
        if kind in {"single", "multiple"} and options:
            for index, option in enumerate(options, 1):
                self._print(f"  {index}. {option}")
            self._print(
                "输入一个选项编号或完整选项文字。" if kind == "single"
                else "输入多个选项编号，用逗号分隔，例如 1,3。"
            )
            if payload.get("allow_custom", True):
                self._print("也可以直接输入自己的回答。")
        if payload.get("recommendation"):
            self._print(f"建议：{payload['recommendation']}（不会自动选择）")
        self._print("=" * 60)

    async def _ask_batch(self, payload: dict[str, Any]) -> str:
        answers: dict[str, str] = {}
        questions = payload.get("questions") or []
        for index, item in enumerate(questions, 1):
            self._print(f"\n[{index}/{len(questions)}] {item.get('header') or ''}".rstrip())
            self._display(item)
            answer = await self._ask(item)
            if not answer and item.get("required", True):
                return ""
            answers[str(item["id"])] = answer
        return json.dumps(answers, ensure_ascii=False)

    async def _ask(self, payload: dict[str, Any]) -> str:
        while True:
            self._print("你的回答> ", end="")
            answer = (await self._readline()).strip()
            if answer == "/cancel":
                self._print("已暂不回答，未提交答案。")
                return ""
            if not answer:
                if not payload.get("required", True):
                    return ""
                self._print("此问题需要明确回答，请输入内容或 /cancel 暂不回答。")
                continue
            normalized = self._normalize_answer(answer, payload)
            if normalized is not None:
                return normalized
            self._print("选项无效，请按显示的编号或完整选项文字重新输入。")

    @staticmethod
    def _normalize_answer(answer: str, payload: dict[str, Any]) -> str | None:
        options = [str(option).strip().casefold() for option in payload.get("options") or []]
        kind = payload.get("type", "custom")
        if not options or kind not in {"single", "multiple"}:
            return answer
        parts = answer.replace("，", ",").split(",") if kind == "multiple" else [answer]
        selections: list[str] = []
        invalid: list[str] = []
        for part in parts:
            part = part.strip()
            if part.isdecimal() and 1 <= int(part) <= len(options):
                selections.append(str(int(part)))
            elif part.casefold() in options:
                selections.append(str(options.index(part.casefold()) + 1))
            else:
                invalid.append(part)
        if not invalid:
            return ",".join(dict.fromkeys(selections))
        # Never silently drop invalid items from a mixed multiple-choice answer,
        # or treat an out-of-range numeric selection as a custom confirmation.
        if (
            not selections and payload.get("allow_custom", True)
            and all(part and not part.isdecimal() for part in invalid)
        ):
            return answer
        return None

    async def _readline(self) -> str:
        if os.name == "nt":
            return await self._readline_windows()
        fd = self.stdin.fileno()
        buffer = bytearray()
        while True:
            # No worker thread blocked in input(): timeout/cancellation must not
            # leave a reader stealing the next prompt or delaying asyncio.run().
            if not select.select([fd], [], [], 0)[0]:
                await asyncio.sleep(0.05)
                continue
            byte = os.read(fd, 1)
            if not byte:
                if not buffer:
                    raise EOFError
                break
            if byte == b"\n":
                break
            buffer.extend(byte)
        return buffer.decode(self.stdin.encoding or "utf-8", errors="replace").rstrip("\r")

    async def _readline_windows(self) -> str:
        import msvcrt

        buffer: list[str] = []
        special_key = False
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.05)
                continue
            char = msvcrt.getwch()
            if special_key:
                special_key = False
            elif char in {"\x00", "\xe0"}:
                special_key = True
            elif char == "\x03":
                raise KeyboardInterrupt
            elif char == "\x1a":
                raise EOFError
            elif char in {"\r", "\n"}:
                self._print()
                return "".join(buffer)
            elif char == "\b":
                if buffer:
                    buffer.pop()
                    self._print("\b \b", end="")
            else:
                buffer.append(char)
                self._print(char, end="")


@contextlib.contextmanager
def terminal_question_handler() -> Iterator[TerminalQuestionHandler]:
    """Keep question prompts on stderr even during fd-level log suppression."""
    output = sys.stderr
    with contextlib.ExitStack() as stack:
        try:
            fd = os.dup(output.fileno())
        except (OSError, ValueError, AttributeError):
            pass  # StringIO and other embedding/test streams have no descriptor.
        else:
            try:
                output = os.fdopen(fd, "w", encoding=output.encoding or "utf-8", errors="replace")
            except BaseException:
                os.close(fd)
                raise
            stack.enter_context(output)
        yield TerminalQuestionHandler(sys.stdin, output)
