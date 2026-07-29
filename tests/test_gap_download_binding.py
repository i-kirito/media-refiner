import tempfile
import unittest
from pathlib import Path

from app import database
from app.routers import gaps


class GapDownloadBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def result(site_name: str, page_url: str) -> dict:
        return {
            "title": "Moonlight Resonance S01 2008 720p MyTVSuper WEB-DL H264 AAC-ADWeb",
            "site_name": site_name,
            "page_url": page_url,
            "size": 20873541059,
        }

    def test_same_release_from_different_sites_has_different_resource_key(self):
        audiences = self.result("观众", "https://audiences.example/details.php?id=626592")
        ttg = self.result("听听歌", "https://ttg.example/t/796596/")

        self.assertNotEqual(gaps._gap_mp_resource_key(audiences), gaps._gap_mp_resource_key(ttg))

    async def test_bound_task_status_only_remains_on_clicked_site(self):
        task_id = "92c62e5b967695acec334a31171733ceb3f3e794"
        audiences = self.result("观众", "https://audiences.example/details.php?id=626592")
        ttg = self.result("听听歌", "https://ttg.example/t/796596/")
        for item in (audiences, ttg):
            item.update({
                "ui_download_task": True,
                "ui_download_status": "stalled",
                "ui_download_label": "等待下载",
                "ui_download_hash": task_id,
            })

        await database.save_gap_download_binding(gaps._gap_mp_resource_key(audiences), task_id)
        annotated = await gaps._apply_gap_download_bindings([audiences, ttg])

        self.assertTrue(annotated[0]["ui_download_task"])
        self.assertEqual(task_id, annotated[0]["ui_download_hash"])
        self.assertNotIn("ui_download_task", annotated[1])
        self.assertNotIn("ui_download_hash", annotated[1])

    async def test_rebinding_same_task_moves_status_to_latest_clicked_resource(self):
        task_id = "same-task"
        first_key = gaps._gap_mp_resource_key(self.result("观众", "https://audiences.example/details.php?id=1"))
        second_key = gaps._gap_mp_resource_key(self.result("听听歌", "https://ttg.example/t/2/"))

        await database.save_gap_download_binding(first_key, task_id)
        await database.save_gap_download_binding(second_key, task_id)
        bindings = await database.list_gap_download_bindings([first_key, second_key], [task_id])

        self.assertNotIn(first_key, bindings)
        self.assertEqual(task_id, bindings[second_key]["task_id"])

    async def test_successful_submission_captures_task_binding_from_downloader_status(self):
        result = self.result("观众", "https://audiences.example/details.php?id=626592")

        class FakeMoviePilot:
            async def annotate_downloader_statuses(self, results):
                return [{
                    **results[0],
                    "ui_download_task": True,
                    "ui_download_hash": "captured-task",
                    "ui_download_name": "Moonlight.Resonance.S01",
                    "ui_downloader": "115",
                    "ui_downloader_type": "qbittorrent",
                }]

        binding = await gaps._save_gap_download_binding_for_result(FakeMoviePilot(), result, {"success": True})
        saved = await database.list_gap_download_bindings(
            [gaps._gap_mp_resource_key(result)],
            ["captured-task"],
        )

        self.assertEqual("captured-task", binding["task_id"])
        self.assertEqual("captured-task", saved[binding["resource_key"]]["task_id"])


if __name__ == "__main__":
    unittest.main()
