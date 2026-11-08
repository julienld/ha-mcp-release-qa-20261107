# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Inspect/delete only the container packages created by the dedicated QA repo."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

SOURCE = "julienld/ha-mcp-release-qa-20261107"
NAMES = ("ha-mcp-release-qa-20261107", "ha-mcp-release-qa-20261107-addon-amd64")


def package_request(
    name: str, *, delete: bool = False, versions: bool = False
) -> object:
    if name not in NAMES:
        raise ValueError("Package outside the exact QA allowlist")
    endpoint = f"users/julienld/packages/container/{name}" + (
        "/versions?per_page=100" if versions else ""
    )
    result = subprocess.run(
        ["gh", "api", "--method", "DELETE" if delete else "GET", endpoint],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        if not delete and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else None


def allowed_package(metadata: dict, expected: str) -> None:
    if (
        expected not in NAMES
        or metadata.get("name") != expected
        or metadata.get("repository", {}).get("full_name") != SOURCE
    ):
        raise ValueError(
            "Container package identity or linked repository is not the QA source"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    if (
        os.environ.get("GITHUB_REPOSITORY") != SOURCE
        or os.environ.get("GITHUB_ACTIONS") != "true"
    ):
        raise ValueError(
            "Only the dedicated QA source workflow may use its package credentials"
        )
    if args.delete and os.environ.get("QA_ARMED") != "false":
        raise ValueError("QA publication must be disarmed before package deletion")
    receipts = []
    for name in NAMES:
        metadata = package_request(name)
        if metadata is None:
            receipts.append({"name": name, "absent": True})
            continue
        allowed_package(metadata, name)
        versions = package_request(name, versions=True)
        value = {
            "name": name,
            "visibility": metadata.get("visibility"),
            "repository": metadata["repository"]["full_name"],
            "versions": [
                {
                    "id": v["id"],
                    "name": v["name"],
                    "tags": v.get("metadata", {}).get("container", {}).get("tags", []),
                }
                for v in versions
            ],
        }
        if args.delete:
            package_request(name, delete=True)
            assert package_request(name) is None, (
                "Deleted QA package is still accessible"
            )
            value["deleted"] = True
        receipts.append(value)
    Path("qa-container-retirement.json").write_text(
        json.dumps({"delete_requested": args.delete, "packages": receipts}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "delete_requested": args.delete,
                "packages": [
                    {
                        "name": v["name"],
                        "deleted": v.get("deleted", False),
                        "absent": v.get("absent", False),
                    }
                    for v in receipts
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
