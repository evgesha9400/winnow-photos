"""
tests/test_scanner.py - Unit tests for studio/scanner.py hierarchy and level extraction.
"""

import tempfile
import unittest
from pathlib import Path
from studio.scanner import scan_directory


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Create hierarchy:
        # root/
        #   unassigned.jpg
        #   Travel/
        #     vacation.jpg
        #     Spain 2021/
        #       barcelona.jpg
        #   Events/
        #     Birthday/
        #       cake.jpg
        #   Trash/
        #     bad.jpg

        (self.root / "unassigned.jpg").touch()

        travel_dir = self.root / "Travel"
        travel_dir.mkdir()
        (travel_dir / "vacation.jpg").touch()

        spain_dir = travel_dir / "Spain 2021"
        spain_dir.mkdir()
        (spain_dir / "barcelona.jpg").touch()

        events_dir = self.root / "Events" / "Birthday"
        events_dir.mkdir(parents=True)
        (events_dir / "cake.jpg").touch()

        trash_dir = self.root / "Trash"
        trash_dir.mkdir()
        (trash_dir / "bad.jpg").touch()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scan_hierarchy_extraction(self):
        state = scan_directory(self.root)

        # Check clusters
        t1_clusters = state['tier1_clusters']
        self.assertIn("Travel", t1_clusters)
        self.assertIn("Events", t1_clusters)
        self.assertIn("Review & Unassigned", t1_clusters)
        self.assertIn("Trash / To Delete", t1_clusters)

        # Check subclusters
        t2_clusters = state['tier2_clusters']
        self.assertEqual(t2_clusters.get("Travel"), ["Spain 2021"])
        self.assertEqual(t2_clusters.get("Events"), ["Birthday"])

        # Check items
        items = state['items']
        self.assertEqual(len(items), 5)

        # 1. Root item
        self.assertEqual(items["unassigned.jpg"]["tier1_cluster"], "Review & Unassigned")
        self.assertIsNone(items["unassigned.jpg"]["tier2_cluster"])
        self.assertFalse(items["unassigned.jpg"]["tier1_reviewed"])

        # 2. Level 1 item
        self.assertEqual(items["vacation.jpg"]["tier1_cluster"], "Travel")
        self.assertIsNone(items["vacation.jpg"]["tier2_cluster"])
        self.assertTrue(items["vacation.jpg"]["tier1_reviewed"])
        self.assertFalse(items["vacation.jpg"]["tier2_reviewed"])

        # 3. Level 2 item
        self.assertEqual(items["barcelona.jpg"]["tier1_cluster"], "Travel")
        self.assertEqual(items["barcelona.jpg"]["tier2_cluster"], "Spain 2021")
        self.assertTrue(items["barcelona.jpg"]["tier1_reviewed"])
        self.assertTrue(items["barcelona.jpg"]["tier2_reviewed"])

        # 4. Events Level 2 item
        self.assertEqual(items["cake.jpg"]["tier1_cluster"], "Events")
        self.assertEqual(items["cake.jpg"]["tier2_cluster"], "Birthday")
        self.assertTrue(items["cake.jpg"]["tier1_reviewed"])
        self.assertTrue(items["cake.jpg"]["tier2_reviewed"])

        # 5. Trash item
        self.assertEqual(items["bad.jpg"]["tier1_cluster"], "Trash / To Delete")


if __name__ == '__main__':
    unittest.main()
