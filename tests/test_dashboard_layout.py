import re
import unittest
from pathlib import Path


DASHBOARD_TEMPLATE = Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"


class DashboardLayoutMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")

    def test_page_shell_and_grid_keep_responsive_layout_contract(self):
        shell = re.search(r'<div class="(dashboard-page[^\"]*)">', self.html)
        self.assertIsNotNone(shell)
        shell_classes = shell.group(1).split()
        for class_name in (
            "w-full",
            "lg:w-[90%]",
            "max-w-[100rem]",
            "mx-auto",
            "px-4",
            "md:px-6",
            "xl:px-8",
        ):
            self.assertIn(class_name, shell_classes)
        self.assertRegex(
            self.html,
            r"#dashboard-grid\s*\{[^}]*display:\s*grid;[^}]*"
            r"grid-template-columns:\s*repeat\(var\(--dashboard-cols,\s*1\),\s*minmax\(0,\s*1fr\)\)",
        )
        for width, columns in ((1120, 4), (920, 3), (620, 2)):
            self.assertIn(f"if (w >= {width}) return {columns};", self.html)
        self.assertIn("return 1;", self.html)
        self.assertRegex(
            self.html,
            r"window\.addEventListener\('resize',[\s\S]*?Layout\.apply\(\)",
        )

    def test_stats_widget_keeps_four_metrics_and_four_column_wide_layout(self):
        stats = re.search(
            r'<div class="widget-item"\s+data-module="stats"[\s\S]*?'
            r'(?=<div class="widget-item"\s+data-module="charts")',
            self.html,
        )
        self.assertIsNotNone(stats)
        markup = stats.group(0)
        self.assertIn('data-default-size="4x1"', markup)
        self.assertEqual(4, len(re.findall(r'class="dashboard-metric"', markup)))
        for metric_id in ("stat-total", "stat-4k", "stat-hevc", "stat-dv"):
            self.assertEqual(1, len(re.findall(rf'id="{metric_id}"', markup)))
        self.assertRegex(
            self.html,
            r"\.dashboard-stat-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)",
        )
        self.assertRegex(
            self.html,
            r"@container\s*\(min-width:\s*520px\)[\s\S]*?"
            r"\.dashboard-stat-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)",
        )

    def test_module_inventory_order_and_default_spans_are_preserved(self):
        modules = re.findall(
            r'class="widget-item"\s+data-module="([^"]+)"\s+data-default-size="([^"]+)"',
            self.html,
        )
        self.assertEqual(
            [
                ("stats", "4x1"),
                ("charts", "2x1"),
                ("hdr", "1x1"),
                ("score", "1x1"),
                ("libraries", "2x1"),
                ("poor", "2x1"),
            ],
            modules,
        )
        self.assertIn(
            "const MODULE_ORDER = ['stats', 'poor', 'charts', 'score', 'hdr', 'libraries'];",
            self.html,
        )
        self.assertIn(
            "widths: { stats: 4, poor: 2, charts: 2, score: 1, hdr: 1, libraries: 2 }",
            self.html,
        )

    def test_primary_controls_and_data_targets_remain_available(self):
        controls = {
            "btn-start-scan": "ScanManager.start()",
            "scheduleToggle": "toggleScheduleDropdown()",
        }
        for element_id, handler in controls.items():
            tag = re.search(rf"<button[^>]*id=\"{element_id}\"[^>]*>", self.html)
            self.assertIsNotNone(tag)
            self.assertIn(handler, tag.group(0))

        for element_id in (
            "scheduleDropdown",
            "scan-progress",
            "loading",
            "dashboard-content",
            "dashboard-grid",
            "resolution-chart",
            "codec-chart",
            "score-histogram",
            "library-dist",
            "poor-items-preview",
        ):
            self.assertEqual(1, len(re.findall(rf'id="{element_id}"', self.html)))

        self.assertRegex(self.html, r'<a\s+href="/items"[^>]*>[\s\S]*?查看全部')


if __name__ == "__main__":
    unittest.main()
