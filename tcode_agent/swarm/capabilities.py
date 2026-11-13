"""Inspect optional scheduler dependencies without importing the Swarms runtime."""

import os
from importlib.machinery import PathFinder


def auto_swarm_builder_availability() -> dict[str, object]:
    if os.environ.get("TCODE_USE_SWARMS", "1").strip().lower() in {"0", "false", "off", "no"}:
        return {"available": False, "reason": "服务配置已关闭扩展蜂群引擎（TCODE_USE_SWARMS）。"}
    search_path = None
    for module in ("swarms", "swarms.structs", "swarms.structs.auto_swarm_builder"):
        try:
            spec = PathFinder.find_spec(module, search_path)
        except (ImportError, ValueError, OSError):
            spec = None
        if spec is None:
            reason = (
                "当前服务的 Python 环境未安装可选 Swarms 依赖。"
                if module == "swarms"
                else "当前 Swarms 安装缺少 AutoSwarmBuilder 模块。"
            )
            return {"available": False, "reason": reason}
        search_path = spec.submodule_search_locations
    return {"available": True, "reason": ""}
