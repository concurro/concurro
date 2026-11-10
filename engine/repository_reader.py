"""Pinned repository reads and the invocation-local tree cache."""

import base64

import candidates
from policy import Blocked


class RepositoryReader:
    """Read approved base objects upstream and candidate objects from the fork."""

    def __init__(self, code, pulls):
        """Keep the two read clients and a tree cache shared by this invocation."""
        self.code, self.pulls = code, pulls
        self.trees = {}

    def source(self, state, sha):
        """Read the approved base from upstream and published candidates from the fork."""
        return self.pulls if not state or sha == state.get("base") else self.code

    def tree(self, state, sha):
        """Fetch a complete tree; never silently treat truncation as missing files."""
        if sha in self.trees:
            return self.trees[sha]
        result = self.source(state, sha).repo(f"git/trees/{sha}?recursive=1")
        if result.get("truncated"):
            raise Blocked("repository tree is truncated")
        self.trees[sha] = {v["path"]: v for v in result["tree"]}
        return self.trees[sha]

    def changed_paths(self, state, base, head):
        """Compare complete pinned trees, including deletions, rename sides and mode changes."""
        before, after = (
            {path: entry for path, entry in self.tree(state, sha).items() if entry["type"] != "tree"}
            for sha in (base, head)
        )

        def identity(entry):
            return tuple(entry.get(key) for key in ("sha", "mode", "type")) if entry else None

        return sorted(
            path for path in before.keys() | after.keys() if identity(before.get(path)) != identity(after.get(path))
        )

    def pr_paths(self, state, number, head=None):
        """Use GitHub's complete PR list, or pinned trees after proving the capped list's merge base."""
        files = self.pulls.pages(f"pulls/{number}/files")
        if len(files) < 3000:
            return candidates.changed_paths(files)
        pr = self.pulls.repo(f"pulls/{number}")
        head = head or state["head"]
        if pr["head"]["sha"] != head:
            raise Blocked("PR changed while reading its complete file inventory")
        # Compare's file list is capped at 300; only its merge-base identity is used.
        comparison = self.pulls.repo(f"compare/{pr['base']['sha']}...{head}")
        if comparison.get("merge_base_commit", {}).get("sha") != state["base"]:
            raise Blocked("cannot prove the complete PR inventory against the approved base")
        return self.changed_paths(state, state["base"], head)

    def file_manifest(self, state, sha, paths):
        """Identify every planned file at a pinned commit; agents read its contents from Git."""
        tree = self.tree(state, sha)
        return {
            path: (
                {key: tree[path][key] for key in ("sha", "size", "mode", "type") if key in tree[path]}
                if path in tree
                else None
            )
            for path in paths
        }

    def files(self, state, sha, paths, inspection=False):
        """Read regular UTF-8 blobs without imposing a source-file size policy."""
        tree = self.tree(state, sha)
        result = {}
        for path in paths:
            entry = tree.get(path)
            if entry is None:
                # Existing symlink/file ancestors must not be treated as new directories.
                ancestors = ["/".join(path.split("/")[:i]) for i in range(1, len(path.split("/")))]
                if any(a in tree and tree[a]["type"] != "tree" for a in ancestors):
                    raise Blocked("non-directory path ancestor")
                result[path] = None
                continue
            modes = ("100644", "100755") if inspection else ("100644",)
            if entry["mode"] not in modes or entry["type"] != "blob":
                raise Blocked("source must be a regular text file")
            blob = self.source(state, sha).repo("git/blobs/" + entry["sha"])
            if blob["encoding"] != "base64":
                raise Blocked("unsupported blob encoding")
            data = base64.b64decode(blob["content"])
            if b"\x00" in data:
                raise Blocked("source is binary")
            result[path] = data.decode("utf-8")
        return result
