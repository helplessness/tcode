"""源码脚本对正式 TCode tracker token API 的兼容入口。"""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tcode_agent.runtime.tracker_token import *
from tcode_agent.runtime.tracker_token import _main


if __name__ == "__main__":
    raise SystemExit(_main())
