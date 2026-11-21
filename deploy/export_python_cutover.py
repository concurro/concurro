"""Export stopped Python and Exchange identity floors for Rust cutover.

All SQLite inputs are opened read-only. The operator supplies the confirmed daemon
exit time and last acknowledged ingress source identity.
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def readonly(path: Path) -> sqlite3.Connection:
    """Open one stopped database without creating or migrating it."""
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def export(
    exchange_path: Path,
    index_path: Path,
    registry_path: Path,
    activation_at: str,
    incarnation: str,
    sequence: int,
) -> dict:
    """Collect exact generation, slot and historical PR identities."""
    datetime.fromisoformat(activation_at.replace("Z", "+00:00"))
    if sequence < 0 or not incarnation:
        raise ValueError("source identity is incomplete")
    with readonly(exchange_path) as exchange, readonly(index_path) as index, readonly(registry_path) as registry:
        repositories = {row["name"]: row["id"] for row in index.execute("SELECT id,name FROM repositories")}
        targets = [
            {
                "repository_id": identifier,
                "activation_at": activation_at,
                "source_incarnation": incarnation,
                "source_sequence": sequence,
            }
            for identifier in sorted(repositories.values())
        ]
        floor_rows = {}
        for row in exchange.execute("SELECT repository,issue,generation FROM funding_generations"):
            key = (row["repository"], row["issue"])
            floor_rows[key] = {"generation": row["generation"], "openai_epoch": -1, "anthropic_epoch": -1}
        for row in exchange.execute("SELECT repository,issue,provider,generation,epoch FROM sponsorship"):
            key = (row["repository"], row["issue"])
            floor = floor_rows.setdefault(
                key, {"generation": row["generation"], "openai_epoch": -1, "anthropic_epoch": -1}
            )
            floor["generation"] = max(floor["generation"], row["generation"])
            if row["provider"] not in ("openai", "anthropic"):
                raise ValueError("unknown provider in Exchange floor")
            floor[f"{row['provider']}_epoch"] = row["epoch"]
        floors = []
        for (repository, issue), floor in sorted(floor_rows.items()):
            if repository not in repositories:
                raise ValueError(f"Exchange floor repository absent from target index: {repository}")
            floors.append({"repository_id": repositories[repository], "issue_number": issue, **floor})
        standalone = {
            (row["pull_repository"].lower(), row["pull"])
            for row in registry.execute(
                "SELECT p.pull_repository,p.pull FROM pulls p "
                "JOIN tasks t ON t.repository=p.repository AND t.issue=p.issue "
                "WHERE t.kind='standalone_pr_review'"
            )
        }
        pulls = {}
        for row in index.execute("SELECT repository,number,document FROM pulls"):
            key = (row["repository"].lower(), row["number"])
            if key in standalone:
                continue
            pulls[key] = {
                "document": json.loads(row["document"]) if row["document"] else {},
                "owned_contribution": False,
            }
        for row in registry.execute("SELECT pull_repository,pull FROM pulls"):
            key = (row["pull_repository"].lower(), row["pull"])
            if key in standalone:
                continue
            pulls.setdefault(key, {"document": {}, "owned_contribution": False})["owned_contribution"] = True
        historical = [
            {"upstream": repository, "pr_number": number, **record}
            for (repository, number), record in sorted(pulls.items())
        ]
    return {"targets": targets, "floors": floors, "pull_requests": historical}


def main() -> None:
    """Write a deterministic private manifest for the stopped Rust importer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exchange_db", type=Path)
    parser.add_argument("list_index_db", type=Path)
    parser.add_argument("registry_db", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--activation-at", required=True)
    parser.add_argument("--source-incarnation", required=True)
    parser.add_argument("--source-sequence", required=True, type=int)
    args = parser.parse_args()
    document = export(
        args.exchange_db,
        args.list_index_db,
        args.registry_db,
        args.activation_at,
        args.source_incarnation,
        args.source_sequence,
    )
    args.manifest.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
