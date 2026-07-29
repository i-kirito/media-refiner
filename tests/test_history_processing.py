import unittest
from pathlib import Path


class HistoryProcessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (Path(__file__).parents[1] / "app" / "templates" / "history.html").read_text()

    def test_processing_transfer_uses_running_category_without_restore_action(self):
        self.assertIn("item.status === 'processing'", self.template)
        self.assertIn("'processing': { label: '转存处理中' }", self.template)
        self.assertIn("item.status !== 'processing'", self.template)

    def test_completed_review_is_hidden_when_execution_log_exists(self):
        self.assertIn("i.sourceKind === 'review' && i.status === 'approved'", self.template)


if __name__ == "__main__":
    unittest.main()
