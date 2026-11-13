"""聊天入口的确定性输入长度边界。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


_DEFAULT_MAX_FRAME_CHARS = 1_000_000
_DEFAULT_MAX_MESSAGE_CHARS = 262_144
_DEFAULT_MAX_LINE_CHARS = 65_536


def _bounded_env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """读取有上下界的整数环境变量，非法值安全回退到默认值。

    参数:
        name: 环境变量名称。
        default: 缺失或非法时使用的默认值。
        minimum: 允许的最小值。
        maximum: 允许的最大值。

    返回:
        被限制在指定范围内的整数。
    """

    try:
        value = int(str(os.environ.get(name, default) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ChatInputLimits:
    """描述一次聊天请求可接受的确定性字符预算。"""

    max_frame_chars: int
    max_message_chars: int
    max_line_chars: int

    @classmethod
    def from_environment(cls) -> "ChatInputLimits":
        """从运行环境构造输入边界，并为错误配置提供安全回退。"""

        max_message_chars = _bounded_env_int(
            "TCODE_CHAT_MAX_MESSAGE_CHARS",
            _DEFAULT_MAX_MESSAGE_CHARS,
            minimum=16_384,
            maximum=1_000_000,
        )
        max_line_chars = _bounded_env_int(
            "TCODE_CHAT_MAX_LINE_CHARS",
            _DEFAULT_MAX_LINE_CHARS,
            minimum=4_096,
            maximum=max_message_chars,
        )
        max_frame_chars = _bounded_env_int(
            "TCODE_CHAT_MAX_FRAME_CHARS",
            _DEFAULT_MAX_FRAME_CHARS,
            minimum=max_message_chars + 4_096,
            maximum=4_000_000,
        )
        return cls(
            max_frame_chars=max_frame_chars,
            max_message_chars=max_message_chars,
            max_line_chars=max_line_chars,
        )


class ChatInputLimitError(ValueError):
    """表示聊天输入在昂贵预处理前被确定性拒绝。"""

    def __init__(self, *, code: str, actual: int, limit: int, message: str):
        """保存稳定错误分类以及便于前端展示的长度信息。"""

        super().__init__(message)
        self.code = str(code)
        self.actual = max(0, int(actual))
        self.limit = max(0, int(limit))

    def as_websocket_event(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """生成失败关闭的 WebSocket 终态事件，避免前端继续等待。"""

        detail = str(self)
        return {
            "type": "error",
            "code": self.code,
            "error": detail,
            "content": detail,
            "actual": self.actual,
            "limit": self.limit,
            "session_id": str(session_id or ""),
            "request_id": str(request_id or ""),
            "retryable": False,
            "done": True,
        }


def validate_chat_frame(
    raw_frame: Any,
    *,
    limits: ChatInputLimits | None = None,
) -> str:
    """在 JSON 解析前验证 WebSocket 文本帧，限制解析成本。

    参数:
        raw_frame: WebSocket 接收到的原始文本帧。
        limits: 可选的显式边界，主要用于测试和受控部署。

    返回:
        验证通过的原始文本帧。

    异常:
        ChatInputLimitError: 帧不是字符串或超过字符预算。
    """

    if not isinstance(raw_frame, str):
        raise ChatInputLimitError(
            code="invalid_chat_frame",
            actual=0,
            limit=0,
            message="聊天请求必须使用文本帧。",
        )
    resolved = limits or ChatInputLimits.from_environment()
    actual = len(raw_frame)
    if actual > resolved.max_frame_chars:
        raise ChatInputLimitError(
            code="chat_frame_too_large",
            actual=actual,
            limit=resolved.max_frame_chars,
            message=(
                f"聊天请求过大：当前 {actual} 个字符，"
                f"最多允许 {resolved.max_frame_chars} 个字符。"
            ),
        )
    return raw_frame


def validate_chat_message(
    message: Any,
    *,
    limits: ChatInputLimits | None = None,
) -> str:
    """以线性扫描验证消息总长和单行长度。

    该函数必须在路由正则、资源识别、研究投影和模型提示构建之前调用。
    它不复制或拆分完整消息，因此大输入的额外内存开销保持常量级。

    参数:
        message: 用户提交的消息内容。
        limits: 可选的显式边界，主要用于测试和受控部署。

    返回:
        验证通过的原始消息。

    异常:
        ChatInputLimitError: 消息类型非法、总长超限或任意单行超限。
    """

    if not isinstance(message, str):
        raise ChatInputLimitError(
            code="invalid_chat_message",
            actual=0,
            limit=0,
            message="聊天消息必须是字符串。",
        )

    resolved = limits or ChatInputLimits.from_environment()
    actual = len(message)
    if actual > resolved.max_message_chars:
        raise ChatInputLimitError(
            code="chat_message_too_large",
            actual=actual,
            limit=resolved.max_message_chars,
            message=(
                f"消息过长：当前 {actual} 个字符，"
                f"最多允许 {resolved.max_message_chars} 个字符。"
            ),
        )

    line_start = 0
    for index, character in enumerate(message):
        if character not in "\r\n":
            continue
        line_length = index - line_start
        if line_length > resolved.max_line_chars:
            raise ChatInputLimitError(
                code="chat_message_line_too_long",
                actual=line_length,
                limit=resolved.max_line_chars,
                message=(
                    f"消息包含过长单行：当前 {line_length} 个字符，"
                    f"单行最多允许 {resolved.max_line_chars} 个字符。"
                ),
            )
        line_start = index + 1

    final_line_length = actual - line_start
    if final_line_length > resolved.max_line_chars:
        raise ChatInputLimitError(
            code="chat_message_line_too_long",
            actual=final_line_length,
            limit=resolved.max_line_chars,
            message=(
                f"消息包含过长单行：当前 {final_line_length} 个字符，"
                f"单行最多允许 {resolved.max_line_chars} 个字符。"
            ),
        )
    return message
