"""The trace UI preserves the KH Software Factory product name."""
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / '.claude/skills/kh/apps/visualizer'




class DashboardBrandTests(unittest.TestCase):
    def test_header_and_tab_title_use_the_product_name(self):
        self.assertIn('<span class="brand">KH Software Factory</span>', (APP / 'src/App.vue').read_text())
        self.assertIn('<title>KH Software Factory</title>', (APP / 'index.html').read_text())



if __name__ == '__main__':
    unittest.main()
