#!/usr/bin/env python3
"""Generate a deterministic Rust dependency inventory for the Debian package."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    manifest = Path(sys.argv[1]).resolve()
    metadata = json.loads(
        subprocess.check_output(
            [
                "cargo",
                "metadata",
                "--manifest-path",
                str(manifest),
                "--locked",
                "--format-version",
                "1",
            ],
            text=True,
        )
    )
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    root = metadata["resolve"]["root"]
    reachable: set[str] = set()
    pending = [root]
    while pending:
        package_id = pending.pop()
        if package_id in reachable:
            continue
        reachable.add(package_id)
        pending.extend(nodes[package_id]["dependencies"])

    def clean(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    rows = []
    for package_id in reachable - {root}:
        package = packages[package_id]
        source = package.get("repository") or package.get("homepage") or "Cargo registry"
        rows.append(
            (
                package["name"],
                package["version"],
                package.get("license") or "See upstream package",
                source,
            )
        )

    print("# Third-party Rust dependencies")
    print()
    print("This inventory is generated from the locked portal-bridge dependency graph.")
    print()
    print("| Package | Version | License | Source |")
    print("|---|---:|---|---|")
    for name, version, license_name, source in sorted(rows):
        print(
            f"| {clean(name)} | {clean(version)} | {clean(license_name)} | "
            f"<{clean(source)}> |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
