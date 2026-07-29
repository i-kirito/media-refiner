import tempfile
import unittest
from pathlib import Path

from app import database
from app.routers import subscribe


class SubscribeUpgradeCountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_counts_only_confirmed_unique_transfers(self):
        await database.add_subscribe_log("rule-a", "规则 A", "transfer", "媒体 1", "item-1", "转存完成")
        await database.add_subscribe_log("rule-a", "规则 A", "transfer", "媒体 1", "item-1", "重复成功日志")
        await database.add_subscribe_log(
            "rule-a", "规则 A", "auto_approved", "媒体 2", "item-2", "自动转存成功"
        )
        await database.add_subscribe_log(
            "rule-a", "规则 A", "auto_approved", "媒体 3", "item-3", "资源已在 115 中"
        )
        await database.add_subscribe_log(
            "rule-a", "规则 A", "auto_approved", "媒体 4", "item-4", "MP 下载已推送"
        )
        await database.add_subscribe_log("rule-a", "规则 A", "download", "媒体 5", "item-5", "审核通过")
        await database.add_subscribe_log("rule-b", "规则 B", "transfer", "媒体 6", "item-6", "转存完成")
        await database.add_subscribe_log("rule-b", "规则 B", "transfer", "无 ID", "", "转存完成")

        counts = await database.count_confirmed_upgrades_by_rule()

        self.assertEqual({"rule-a": 3, "rule-b": 1}, counts)

    async def test_rules_api_overrides_stale_saved_total(self):
        await database.save_subscribe_rule({"id": "rule-a", "name": "规则 A", "total_upgraded": 99})
        await database.save_subscribe_rule({"id": "rule-b", "name": "规则 B", "total_upgraded": 88})
        await database.add_subscribe_log("rule-a", "规则 A", "transfer", "媒体 1", "item-1", "转存完成")

        response = await subscribe.list_rules()
        rules = {rule["id"]: rule for rule in response["data"]}

        self.assertEqual(1, rules["rule-a"]["total_upgraded"])
        self.assertEqual(0, rules["rule-b"]["total_upgraded"])


if __name__ == "__main__":
    unittest.main()
