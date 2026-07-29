import unittest
from pathlib import Path


class PlansReviewLinkMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project_root = Path(__file__).resolve().parents[1]
        cls.template = (project_root / "app/templates/plans.html").read_text(encoding="utf-8")

    def test_current_file_card_owns_emby_link(self):
        self.assertIn("function safeEmbyItemUrl(itemId)", self.template)
        self.assertIn("const embyItemUrl = safeEmbyItemUrl(r.item_id);", self.template)
        self.assertIn("review-current-link", self.template)
        self.assertNotIn('class="review-emby-link"', self.template)

    def test_emby_link_uses_encoded_fragment_parameters(self):
        self.assertIn("new URLSearchParams({id: String(itemId)})", self.template)
        self.assertIn("params.set('serverId', String(embyServerId))", self.template)
        self.assertIn('rel="noopener noreferrer"', self.template)


if __name__ == "__main__":
    unittest.main()
