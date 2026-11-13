"""插件项目自带的 SDK v1 静态合同测试。"""

import unittest
from pathlib import Path

from tcode_agent.plugin_sdk.testing import contract_test_plugin


class ContractTests(unittest.TestCase):
    """验证当前项目只使用 SDK v1 公开契约。"""

    def test_sdk_contract(self) -> None:
        """要求 Manifest、语法和导入边界全部通过。"""

        self.assertEqual([], contract_test_plugin(Path(__file__).parent))


if __name__ == "__main__":
    unittest.main()
