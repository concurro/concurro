"""Stopped-controller PR ownership export uses registry authority, not list visibility."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from deploy.export_python_cutover import export


class CutoverExportTests(unittest.TestCase):
    """Check the three distinct historical PR roles in one stopped export."""

    def test_only_contribution_registry_pulls_claim_ownership(self):
        """Index-only rows stay historical and standalone reviews stay independent."""
        with tempfile.TemporaryDirectory() as directory:
            exchange_path = Path(directory) / "exchange.db"
            index_path = Path(directory) / "index.db"
            registry_path = Path(directory) / "registry.db"
            with sqlite3.connect(exchange_path) as connection:
                connection.executescript("""
                    CREATE TABLE funding_generations (repository TEXT, issue INTEGER, generation INTEGER);
                    CREATE TABLE sponsorship (repository TEXT, issue INTEGER, provider TEXT,
                        generation INTEGER, epoch INTEGER);
                """)
            with sqlite3.connect(index_path) as connection:
                connection.executescript("""
                    CREATE TABLE repositories (id INTEGER, name TEXT);
                    CREATE TABLE pulls (repository TEXT, number INTEGER, document TEXT);
                    INSERT INTO repositories VALUES (8,'acme/source');
                    INSERT INTO pulls VALUES ('Acme/Source',1,'{}'),('Acme/Source',2,'{}'),
                        ('Acme/Source',3,'{}');
                """)
            with sqlite3.connect(registry_path) as connection:
                connection.executescript("""
                    CREATE TABLE tasks (repository TEXT, issue INTEGER, kind TEXT);
                    CREATE TABLE pulls (repository TEXT, issue INTEGER,
                        pull_repository TEXT, pull INTEGER);
                    INSERT INTO pulls VALUES ('acme/issues',4,'Acme/Source',2),
                        ('acme/issues',5,'Acme/Source',3);
                    INSERT INTO tasks VALUES ('acme/issues',5,'standalone_pr_review');
                """)
            result = export(
                exchange_path,
                index_path,
                registry_path,
                "2026-11-21T10:00:00Z",
                "stream-incarnation",
                10,
            )
        self.assertEqual(
            [(item["upstream"], item["pr_number"], item["owned_contribution"]) for item in result["pull_requests"]],
            [("acme/source", 1, False), ("acme/source", 2, True)],
        )


if __name__ == "__main__":
    unittest.main()
