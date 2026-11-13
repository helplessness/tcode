#!/usr/bin/env python3
"""执行 Skill 内置的 primer_designer 命令行入口。"""

from __future__ import annotations

import sys


def main() -> int:
    """加载同目录中的内置包，并转交全部命令行参数。"""

    try:
        from primer_designer.cli import main as primer_cli
        return primer_cli()
    except ModuleNotFoundError as exc:
        print(
            f"错误：缺少运行依赖 {exc.name!r}。"
            "请在隔离 Python 环境中安装本 Skill 的 requirements.txt。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
