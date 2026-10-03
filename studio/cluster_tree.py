"""
studio/cluster_tree.py - Arbitrary N-Level Cluster Tree Model and Operations.
Supports recursive nesting of clusters to any depth, cascading renames/deletes,
and serialization to/from JSON.
"""

from typing import List, Dict, Any, Optional


class ClusterNode:
    def __init__(self, name: str, parent: Optional['ClusterNode'] = None):
        self.name = name
        self.parent = parent
        self.children: List['ClusterNode'] = []

    def get_path(self) -> List[str]:
        """Return the path from root to this node as a list of strings."""
        path = []
        curr = self
        while curr and curr.parent is not None:
            path.append(curr.name)
            curr = curr.parent
        path.reverse()
        return path

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'children': [c.to_dict() for c in self.children]
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any], parent: Optional['ClusterNode'] = None) -> 'ClusterNode':
        node = cls(name=d.get('name', ''), parent=parent)
        for child_dict in d.get('children', []):
            node.children.append(cls.from_dict(child_dict, parent=node))
        return node


class ClusterTree:
    """Manages an arbitrary depth tree of clusters."""

    def __init__(self, root: Optional[ClusterNode] = None):
        if root:
            self.root = root
        else:
            self.root = ClusterNode("Root")
            # Default standard clusters
            self.ensure_default_clusters()

    def ensure_default_clusters(self):
        """Ensure system clusters exist."""
        if not self.find_node(["Review & Unassigned"]):
            self.root.children.append(ClusterNode("Review & Unassigned", parent=self.root))
        if not self.find_node(["Trash / To Delete"]):
            self.root.children.append(ClusterNode("Trash / To Delete", parent=self.root))

    def find_node(self, path: List[str]) -> Optional[ClusterNode]:
        """Find a node by path (e.g. ['Travel & Holidays', 'Spain', 'Costa Brava'])."""
        if not path:
            return self.root

        curr = self.root
        for part in path:
            found = False
            for child in curr.children:
                if child.name.strip().lower() == part.strip().lower():
                    curr = child
                    found = True
                    break
            if not found:
                return None
        return curr

    def add_path(self, path: List[str]) -> ClusterNode:
        """Add a path to the tree, creating intermediate nodes as necessary."""
        if not path:
            return self.root

        curr = self.root
        for part in path:
            cleaned = part.strip()
            if not cleaned:
                continue
            found = None
            for child in curr.children:
                if child.name.strip().lower() == cleaned.lower():
                    found = child
                    break
            if not found:
                found = ClusterNode(cleaned, parent=curr)
                curr.children.append(found)
            curr = found
        return curr

    def create_cluster(self, parent_path: List[str], name: str) -> Optional[ClusterNode]:
        """Create a child cluster under parent_path."""
        cleaned_name = name.strip()
        if not cleaned_name:
            return None

        parent_node = self.find_node(parent_path)
        if not parent_node:
            # Create parent path if missing
            parent_node = self.add_path(parent_path)

        # Check if already exists under parent
        for child in parent_node.children:
            if child.name.strip().lower() == cleaned_name.lower():
                return child

        new_node = ClusterNode(cleaned_name, parent=parent_node)
        parent_node.children.append(new_node)
        return new_node

    SYSTEM_CLUSTERS = {"review & unassigned", "trash / to delete"}

    @classmethod
    def is_system_cluster(cls, path: List[str]) -> bool:
        """Check if path refers to a protected system cluster case-insensitively."""
        return len(path) == 1 and path[0].strip().lower() in cls.SYSTEM_CLUSTERS

    def rename_cluster(self, target_path: List[str], new_name: str) -> bool:
        """Rename a cluster node. Returns True if successful."""
        cleaned_new_name = new_name.strip()
        if not target_path or not cleaned_new_name:
            return False

        node = self.find_node(target_path)
        if not node or node == self.root or not node.parent:
            return False

        # System clusters cannot be renamed (check case-insensitively)
        if self.is_system_cluster(node.get_path()) or self.is_system_cluster(target_path):
            return False

        # Sibling collision check (case-insensitive)
        for sibling in node.parent.children:
            if sibling is not node and sibling.name.strip().lower() == cleaned_new_name.lower():
                return False

        node.name = cleaned_new_name
        return True

    def delete_cluster(self, target_path: List[str]) -> bool:
        """
        Delete a cluster node and all its descendants.
        Returns True if deleted, False if protected or not found.
        """
        if not target_path:
            return False

        # Protect system clusters case-insensitively
        if self.is_system_cluster(target_path):
            return False

        node = self.find_node(target_path)
        if not node or not node.parent or node == self.root:
            return False

        if self.is_system_cluster(node.get_path()):
            return False

        node.parent.children.remove(node)
        return True

    def move_cluster(self, source_path: List[str], new_parent_path: List[str]) -> bool:
        """
        Move a cluster subtree under a new parent.
        Performs cycle check before creating any new destination nodes.
        Checks for sibling collision under the target parent before moving.
        """
        if not source_path:
            return False

        node = self.find_node(source_path)
        if not node or not node.parent or node == self.root:
            return False

        # Protect system clusters case-insensitively
        if self.is_system_cluster(source_path) or self.is_system_cluster(node.get_path()):
            return False

        canonical_source_path = node.get_path()

        # Cycle check: cannot move into self or own descendant
        # Perform cycle check BEFORE creating any new destination nodes
        if len(new_parent_path) >= len(canonical_source_path):
            prefix = [p.strip().lower() for p in new_parent_path[:len(canonical_source_path)]]
            src_prefix = [p.strip().lower() for p in canonical_source_path]
            if prefix == src_prefix:
                return False

        target_parent = self.find_node(new_parent_path) if new_parent_path else self.root
        if target_parent:
            curr = target_parent
            while curr:
                if curr == node:
                    return False
                curr = curr.parent

            # Check for sibling collision under existing target_parent
            for sibling in target_parent.children:
                if sibling is not node and sibling.name.strip().lower() == node.name.strip().lower():
                    return False

        if target_parent and target_parent == node.parent:
            return True

        if not target_parent:
            target_parent = self.add_path(new_parent_path)

        # Check for sibling collision under target_parent before moving
        for sibling in target_parent.children:
            if sibling is not node and sibling.name.strip().lower() == node.name.strip().lower():
                return False

        # Remove from old parent
        node.parent.children.remove(node)
        # Add to new parent
        node.parent = target_parent
        target_parent.children.append(node)
        return True

    reparent_cluster = move_cluster

    def get_children_names(self, path: List[str]) -> List[str]:
        """Return names of immediate children for a given path."""
        node = self.find_node(path)
        if not node:
            return []
        return [c.name for c in node.children]

    def get_all_paths(self) -> List[List[str]]:
        """Return all valid cluster paths in the tree (excluding root)."""
        paths = []

        def _traverse(node: ClusterNode):
            if node != self.root:
                paths.append(node.get_path())
            for child in node.children:
                _traverse(child)

        _traverse(self.root)
        return paths

    def to_dict(self) -> Dict[str, Any]:
        return self.root.to_dict()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ClusterTree':
        root = ClusterNode.from_dict(d)
        tree = cls(root=root)
        tree.ensure_default_clusters()
        return tree

    @classmethod
    def from_legacy_tiers(cls, tier1: List[str], tier2: Dict[str, List[str]]) -> 'ClusterTree':
        """Construct a tree from legacy 2-tier structures."""
        tree = cls()
        for t1 in tier1:
            t1_node = tree.add_path([t1])
            sub_list = tier2.get(t1, [])
            for sub in sub_list:
                tree.add_path([t1, sub])
        tree.ensure_default_clusters()
        return tree
