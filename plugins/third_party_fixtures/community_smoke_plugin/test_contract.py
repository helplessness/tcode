"""第三方测试插件自己的 SDK 合同与纯业务逻辑测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from tcode_agent.plugin_sdk.testing import contract_test_plugin

from plugin import Plugin


class ContractTests(unittest.IsolatedAsyncioTestCase):
    """验证插件只依赖公共 SDK，且核心纯计算结果保持稳定。"""

    def test_sdk_contract(self) -> None:
        """要求 Manifest、入口语法和公开导入边界全部通过。"""

        self.assertEqual([], contract_test_plugin(Path(__file__).parent))

    def test_metadata_matches_manifest(self) -> None:
        """要求运行时身份与 Manifest 中的名称和版本一致。"""

        self.assertEqual("community-smoke-plugin", Plugin.info.name)
        self.assertEqual("0.1.0", Plugin.info.version)

    async def test_pure_tool_logic(self) -> None:
        """验证回显和字符统计无需宿主特权即可得到确定结果。"""

        plugin = Plugin()
        plugin._prefix = "TEST"
        echoed = await plugin._echo({"text": "hello"}, None)  # type: ignore[arg-type]
        counted = await plugin._count(
            {"text": "hello world", "mode": "words"},
            None,  # type: ignore[arg-type]
        )
        self.assertEqual("TEST: hello", echoed.content)
        self.assertEqual("2", counted.content)
        self.assertEqual({"mode": "words", "count": 2}, counted.metadata)


if __name__ == "__main__":
    unittest.main()
