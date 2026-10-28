#!/usr/bin/env python3
"""Refresh separate README lists of upstream profiles and published JSON files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


START = "<!-- generated-profile-links:start -->"
END = "<!-- generated-profile-links:end -->"
UPSTREAM_BLOB_ROOT = (
    "https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever/blob/release/"
)


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, ""))


def render_rows(manifest: dict) -> str:
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("manifest must contain a non-empty profiles array")

    rows = ["### INCY JSON", "", "| 上游规则 | INCY JSON 链接 |", "| --- | --- |"]
    for profile in profiles:
        source_url = profile["source"]
        source_name = unquote(urlsplit(source_url).path.rsplit("/", 1)[-1])
        source_href = UPSTREAM_BLOB_ROOT + quote(source_name)
        inc_json = profile["json"]
        happ_json = profile["happJson"]
        rows.append(
            "| "
            f"[{source_name}]({source_href}) | "
            f"[{inc_json}]({canonical_url(profile['url'])}) |"
        )

    rows.extend(["", "### Happ JSON", "", "| 上游规则 | Happ JSON 链接 |", "| --- | --- |"])
    for profile in profiles:
        source_url = profile["source"]
        source_name = unquote(urlsplit(source_url).path.rsplit("/", 1)[-1])
        source_href = UPSTREAM_BLOB_ROOT + quote(source_name)
        happ_json = profile["happJson"]
        rows.append(
            "| "
            f"[{source_name}]({source_href}) | "
            f"[{happ_json}]({canonical_url(profile['happUrl'])}) |"
        )
    return "\n".join(rows)


def update_readme(readme_path: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    text = readme_path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if len(pattern.findall(text)) != 1:
        raise ValueError("README must contain exactly one generated profile links block")
    replacement = f"{START}\n{render_rows(manifest)}\n{END}"
    readme_path.write_text(pattern.sub(replacement, text), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    args = parser.parse_args()
    update_readme(args.readme, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
