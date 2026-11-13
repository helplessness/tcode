"""允许通过 ``python -m tcode_agent.plugin_sdk`` 调用官方 Plugin CLI。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
