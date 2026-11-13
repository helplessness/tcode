"""ZIP 第三方测试插件的公开 SDK 合同测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from tcode_agent.plugin_sdk.testing import contract_test_plugin


class ContractTests(unittest.TestCase):
    """验证可打包源码只使用允许的 SDK 与 Manifest 契约。"""

    def test_sdk_contract(self) -> None:
        """要求 Manifest、入口语法和私有导入检查全部通过。"""

        self.assertEqual([], contract_test_plugin(Path(__file__).parent))


if __name__ == "__main__":
    unittest.main()
