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

        # 6. Check cluster_tree exists
        self.assertIn('cluster_tree', state)
        from studio.cluster_tree import ClusterTree
        tree = ClusterTree.from_dict(state['cluster_tree'])
        self.assertIsNotNone(tree.find_node(["Travel", "Spain 2021"]))
        self.assertIsNotNone(tree.find_node(["Events", "Birthday"]))

    def test_deep_nested_hierarchy_extraction(self):
        deep_dir = self.root / "Vacations" / "Europe" / "Spain" / "Costa Brava" / "Tossa"
        deep_dir.mkdir(parents=True)
        (deep_dir / "beach.jpg").touch()

        state = scan_directory(self.root)
        self.assertIn("beach.jpg", state['items'])
        it = state['items']["beach.jpg"]
        self.assertEqual(it['cluster_path'], ["Vacations", "Europe", "Spain", "Costa Brava", "Tossa"])
        self.assertEqual(it['reviewed_depth'], 5)

        from studio.cluster_tree import ClusterTree
        tree = ClusterTree.from_dict(state['cluster_tree'])
        self.assertIsNotNone(tree.find_node(["Vacations", "Europe", "Spain", "Costa Brava", "Tossa"]))


    def test_rescan_preserves_curation_state(self):
        # Initial scan
        state = scan_directory(self.root)
        unassigned_it = state['items']["unassigned.jpg"]
        unassigned_it['status'] = "unassigned"
        unassigned_it['need_review'] = True
        unassigned_it['visual_description'] = "Curated description for unassigned photo"

        vacation_it = state['items']["vacation.jpg"]
        vacation_it['cluster_path'] = ["Travel", "Spain 2021", "Beach"]
        vacation_it['reviewed_depth'] = 3
        vacation_it['status'] = "confirmed"

        # Perform rescan with existing_state provided
        rescanned = scan_directory(self.root, existing_state=state, rescan=True)
        res_unassigned = rescanned['items']["unassigned.jpg"]
        self.assertEqual(res_unassigned['status'], "unassigned")
        self.assertTrue(res_unassigned.get('need_review'))
        self.assertEqual(res_unassigned['visual_description'], "Curated description for unassigned photo")

        res_vacation = rescanned['items']["vacation.jpg"]
        self.assertEqual(res_vacation['cluster_path'], ["Travel", "Spain 2021", "Beach"])
        self.assertEqual(res_vacation['reviewed_depth'], 3)
        self.assertEqual(res_vacation['status'], "confirmed")


if __name__ == '__main__':
    unittest.main()

