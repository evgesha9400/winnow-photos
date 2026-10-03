"""
tests/test_cluster_tree.py - Unit tests for arbitrary N-level cluster tree.
"""

import unittest
from studio.cluster_tree import ClusterNode, ClusterTree


class TestClusterTree(unittest.TestCase):
    def test_tree_construction_and_find(self):
        tree = ClusterTree()
        # Add deep nested path
        tree.add_path(["Travel & Holidays", "Spain", "Costa Brava", "Tossa de Mar"])

        node = tree.find_node(["Travel & Holidays", "Spain", "Costa Brava", "Tossa de Mar"])
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "Tossa de Mar")
        self.assertEqual(node.get_path(), ["Travel & Holidays", "Spain", "Costa Brava", "Tossa de Mar"])

        # Check intermediate children
        spain_children = tree.get_children_names(["Travel & Holidays", "Spain"])
        self.assertIn("Costa Brava", spain_children)

    def test_create_cluster(self):
        tree = ClusterTree()
        tree.create_cluster(["Portraits"], "Studio")
        tree.create_cluster(["Portraits", "Studio"], "Lighting Setup 1")

        node = tree.find_node(["Portraits", "Studio", "Lighting Setup 1"])
        self.assertIsNotNone(node)
        self.assertEqual(node.get_path(), ["Portraits", "Studio", "Lighting Setup 1"])

    def test_rename_cluster(self):
        tree = ClusterTree()
        tree.add_path(["A", "B", "C", "D"])
        ok = tree.rename_cluster(["A", "B", "C"], "NewC")
        self.assertTrue(ok)

        # Node at old path should not exist
        self.assertIsNone(tree.find_node(["A", "B", "C"]))
        # Node at new path should exist
        new_c = tree.find_node(["A", "B", "NewC"])
        self.assertIsNotNone(new_c)
        self.assertEqual(new_c.name, "NewC")

        # Descendant should have updated path
        d = tree.find_node(["A", "B", "NewC", "D"])
        self.assertIsNotNone(d)
        self.assertEqual(d.get_path(), ["A", "B", "NewC", "D"])

    def test_delete_cluster_cascading(self):
        tree = ClusterTree()
        tree.add_path(["A", "B", "C", "D"])
        tree.add_path(["A", "B", "E"])

        # Delete C (and thus D)
        ok = tree.delete_cluster(["A", "B", "C"])
        self.assertTrue(ok)

        self.assertIsNone(tree.find_node(["A", "B", "C"]))
        self.assertIsNone(tree.find_node(["A", "B", "C", "D"]))
        # E still exists
        self.assertIsNotNone(tree.find_node(["A", "B", "E"]))

    def test_move_cluster(self):
        tree = ClusterTree()
        tree.add_path(["Europe", "Spain", "Madrid"])
        tree.add_path(["Americas", "USA"])

        # Move Spain under Americas
        ok = tree.move_cluster(["Europe", "Spain"], ["Americas"])
        self.assertTrue(ok)

        self.assertIsNone(tree.find_node(["Europe", "Spain"]))
        spain = tree.find_node(["Americas", "Spain"])
        self.assertIsNotNone(spain)
        madrid = tree.find_node(["Americas", "Spain", "Madrid"])
        self.assertIsNotNone(madrid)
        self.assertEqual(madrid.get_path(), ["Americas", "Spain", "Madrid"])

    def test_serialization(self):
        tree = ClusterTree()
        tree.add_path(["Level1", "Level2", "Level3"])
        tree_dict = tree.to_dict()

        reloaded = ClusterTree.from_dict(tree_dict)
        node = reloaded.find_node(["Level1", "Level2", "Level3"])
        self.assertIsNotNone(node)
        self.assertEqual(node.get_path(), ["Level1", "Level2", "Level3"])

    def test_legacy_conversion(self):
        tier1 = ["Travel", "Portraits"]
        tier2 = {"Travel": ["Spain", "UK"], "Portraits": ["Studio"]}
        tree = ClusterTree.from_legacy_tiers(tier1, tier2)

        self.assertIsNotNone(tree.find_node(["Travel", "Spain"]))
        self.assertIsNotNone(tree.find_node(["Travel", "UK"]))
        self.assertIsNotNone(tree.find_node(["Portraits", "Studio"]))
        self.assertIsNotNone(tree.find_node(["Review & Unassigned"]))

    def test_get_path_with_root_named_cluster(self):
        tree = ClusterTree()
        # Add a cluster whose name is literally "Root"
        node = tree.add_path(["Travel", "Root", "Sub"])
        self.assertIsNotNone(node)
        self.assertEqual(node.get_path(), ["Travel", "Root", "Sub"])

        root_child = tree.find_node(["Travel", "Root"])
        self.assertIsNotNone(root_child)
        self.assertEqual(root_child.get_path(), ["Travel", "Root"])

        # Top-level cluster named "Root"
        top_root = tree.add_path(["Root", "Child"])
        self.assertEqual(top_root.get_path(), ["Root", "Child"])

    def test_rename_sibling_collision(self):
        tree = ClusterTree()
        tree.add_path(["Europe", "France"])
        tree.add_path(["Europe", "Spain"])

        # Collide with existing sibling
        ok = tree.rename_cluster(["Europe", "France"], "Spain")
        self.assertFalse(ok)

        # Case-insensitive collision check
        ok = tree.rename_cluster(["Europe", "France"], "spain")
        self.assertFalse(ok)

        # Sibling rename of self (case change) should succeed
        ok = tree.rename_cluster(["Europe", "France"], "FRANCE")
        self.assertTrue(ok)
        france_node = tree.find_node(["Europe", "FRANCE"])
        self.assertIsNotNone(france_node)

    def test_move_sibling_collision(self):
        tree = ClusterTree()
        tree.add_path(["Europe", "Madrid"])
        tree.add_path(["Americas", "Madrid"])

        # Try to move Europe/Madrid to Americas (already has Madrid)
        ok = tree.move_cluster(["Europe", "Madrid"], ["Americas"])
        self.assertFalse(ok)

        # Europe/Madrid should still exist
        self.assertIsNotNone(tree.find_node(["Europe", "Madrid"]))

    def test_move_cycle_prevention(self):
        tree = ClusterTree()
        tree.add_path(["Europe", "Spain", "Madrid"])

        # Moving Europe into Spain (descendant)
        ok = tree.move_cluster(["Europe"], ["Europe", "Spain"])
        self.assertFalse(ok)

        # Moving Spain into Spain (self)
        ok = tree.move_cluster(["Europe", "Spain"], ["Europe", "Spain"])
        self.assertFalse(ok)

        # Moving Spain into non-existent deep descendant (must not create intermediate nodes)
        ok = tree.reparent_cluster(["Europe", "Spain"], ["Europe", "Spain", "Madrid", "Barrio", "Street"])
        self.assertFalse(ok)
        self.assertIsNone(tree.find_node(["Europe", "Spain", "Madrid", "Barrio"]))

    def test_system_clusters_protection_case_insensitive(self):
        tree = ClusterTree()
        # Delete protection
        self.assertFalse(tree.delete_cluster(["Review & Unassigned"]))
        self.assertFalse(tree.delete_cluster(["review & unassigned"]))
        self.assertFalse(tree.delete_cluster(["Trash / To Delete"]))
        self.assertFalse(tree.delete_cluster(["trash / to delete"]))

        # Rename protection
        self.assertFalse(tree.rename_cluster(["Review & Unassigned"], "Inbox"))
        self.assertFalse(tree.rename_cluster(["review & unassigned"], "Inbox"))
        self.assertFalse(tree.rename_cluster(["trash / to delete"], "Bin"))

        # Move protection
        self.assertFalse(tree.move_cluster(["Review & Unassigned"], ["SomeWhere"]))
        self.assertFalse(tree.move_cluster(["trash / to delete"], ["SomeWhere"]))


if __name__ == '__main__':
    unittest.main()
