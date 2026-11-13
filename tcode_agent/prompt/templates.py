"""从安装包加载固定提示词；动态上下文仍由现有组装器生成。"""

from functools import lru_cache
from importlib.resources import files
import re


@lru_cache(maxsize=32)
def load_prompt_template(name: str) -> str:
    """加载受控的内建 Markdown，缺失或空文件时拒绝继续组装。

    资源随 Python 包分发，不依赖工作目录或宿主机绝对路径。
    缓存固定正文直到进程重启；仅去掉文件末尾的一个换行，保持迁移前正文。
    """

    if not re.fullmatch(r"[a-z][a-z0-9-]*\.md", name):
        raise ValueError(f"无效的内建提示词名称：{name!r}")
    try:
        content = files("tcode_agent.prompt").joinpath("templates", name).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法加载内建提示词 {name}，请检查安装包资源") from exc
    if not content.strip():
        raise RuntimeError(f"内建提示词 {name} 为空，已停止组装")
    return content.removesuffix("\n")
