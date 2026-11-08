"""Pinned repository reads and the invocation-local tree cache."""

import base64

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
