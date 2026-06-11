#!/usr/bin/env python3
"""Refresh the profile README Open Source Contributions section.

Curated first, automated second:
- Put selected repositories in oss_contributions.json.
- The script refreshes stars through GitHub's repos API via `gh api`.
- It refreshes merged PR counts through GitHub GraphQL search via `gh api graphql`.
- README.md is rewritten only between OSS_START / OSS_END markers.

This intentionally avoids a hosted badge service so the profile stays portable.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "oss_contributions.json"
README_PATH = ROOT / "README.md"
START_MARKER = "<!-- OSS_START -->"
END_MARKER = "<!-- OSS_END -->"


@dataclass(frozen=True)
class RepoSpec:
    owner: str
    name: str
    description: str = ""
    notes_markdown: str = ""
    category: str = "Selected OSS Contributions"

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.name}"


def run_gh(args: list[str], timeout: int = 30) -> str | None:
    try:
        result = subprocess.run(
            ["gh", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        print("[ERROR] GitHub CLI `gh` is required.", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[WARN] gh {' '.join(args[:3])} failed: {exc}", file=sys.stderr)
        return None

    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        print(f"[WARN] gh {' '.join(args[:4])} returned {result.returncode}: {msg[:200]}", file=sys.stderr)
        return None
    return result.stdout


def gh_json(args: list[str], timeout: int = 30) -> Any | None:
    out = run_gh(args, timeout=timeout)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # `gh --jq` may output a scalar rather than JSON text.
        stripped = out.strip()
        if stripped.isdigit():
            return int(stripped)
        return stripped


def fetch_repo(owner: str, name: str) -> dict[str, Any] | None:
    data = gh_json(["api", f"repos/{owner}/{name}"], timeout=30)
    return data if isinstance(data, dict) else None


def fetch_merged_pr_count(owner: str, name: str, author: str) -> int | None:
    query = f"repo:{owner}/{name} is:pr author:{author} is:merged"
    gql = "query($q: String!) { search(query: $q, type: ISSUE, first: 0) { issueCount } }"
    data = gh_json(
        [
            "api",
            "graphql",
            "-f",
            f"q={query}",
            "-f",
            f"query={gql}",
            "--jq",
            ".data.search.issueCount",
        ],
        timeout=30,
    )
    return int(data) if isinstance(data, int) else None


def discover_repos(author: str, limit: int = 100) -> list[RepoSpec]:
    """Best-effort discovery for a fresh profile before curation exists."""
    query = f"is:pr author:{author} is:merged -user:{author}"
    data = gh_json(
        [
            "search",
            "prs",
            query,
            "--json",
            "repository,title,url,closedAt",
            "--limit",
            str(limit),
        ],
        timeout=60,
    )
    if not isinstance(data, list):
        return []

    repos: dict[str, RepoSpec] = {}
    for pr in data:
        repo = ((pr or {}).get("repository") or {}).get("nameWithOwner")
        if not repo or "/" not in repo:
            continue
        owner, name = repo.split("/", 1)
        repos[repo] = RepoSpec(owner=owner, name=name, category="Discovered Contributions")
    return sorted(repos.values(), key=lambda r: r.key.lower())


def load_config() -> tuple[str, list[RepoSpec], bool]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    username = config.get("username") or "handlecusion"
    auto_discover = bool(config.get("auto_discover_when_empty", True))
    specs: list[RepoSpec] = []

    for category in config.get("categories", []):
        category_name = category.get("name", "Selected OSS Contributions")
        for repo in category.get("repos", []):
            owner = repo.get("owner")
            name = repo.get("name")
            if not owner or not name:
                continue
            specs.append(
                RepoSpec(
                    owner=owner,
                    name=name,
                    description=repo.get("description", ""),
                    notes_markdown=repo.get("notes_markdown", ""),
                    category=category_name,
                )
            )
    if not specs and auto_discover:
        specs = discover_repos(username)
    return username, specs, auto_discover


def format_stars(n: int | None) -> str:
    return "—" if n is None else f"⭐ {n:,}"


def markdown_escape_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def repo_description(spec: RepoSpec, repo_data: dict[str, Any] | None) -> str:
    if spec.description:
        return spec.description
    if repo_data and repo_data.get("description"):
        return str(repo_data["description"])
    return "—"


def build_row(spec: RepoSpec, author: str, stars: int | None, merged: int | None, desc: str) -> str:
    pr_query = f"https://github.com/{spec.key}/pulls?q=author%3A{author}+is%3Amerged"
    project = f'[{spec.key}](https://github.com/{spec.key})'
    merged_cell = f"[{merged if merged is not None else '—'}]({pr_query})"
    notes = spec.notes_markdown or ""
    return "| {project} | {desc} | {stars} | {merged} | {notes} |".format(
        project=project,
        desc=markdown_escape_cell(desc),
        stars=format_stars(stars),
        merged=merged_cell,
        notes=notes,
    )


def build_section(username: str, specs: list[RepoSpec]) -> str:
    rows_by_category: dict[str, list[tuple[int, int, str]]] = {}

    for spec in specs:
        repo_data = fetch_repo(spec.owner, spec.name)
        raw_stars = repo_data.get("stargazers_count") if repo_data else None
        stars = raw_stars if isinstance(raw_stars, int) else None
        merged = fetch_merged_pr_count(spec.owner, spec.name, username)
        desc = repo_description(spec, repo_data)

        row = build_row(spec, username, stars, merged, desc)
        rows_by_category.setdefault(spec.category, []).append((merged or 0, stars or 0, row))
        print(f"[INFO] {spec.key}: stars={stars} merged_prs={merged}")

    lines: list[str] = [""]
    if specs:
        for category, rows in rows_by_category.items():
            lines.extend(
                [
                    f"### {category}",
                    "",
                    "| Project | Description | Stars | Merged PRs | Notes |",
                    "|:--|:--|--:|:-:|:--|",
                ]
            )
            for _merged, _stars, row in sorted(rows, key=lambda item: (-item[0], -item[1])):
                lines.append(row)
            lines.append("")
    else:
        lines.extend(
            [
                "No external merged PRs found yet. Add target repositories to `oss_contributions.json` after contributions land.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def replace_readme(section: str) -> None:
    text = README_PATH.read_text(encoding="utf-8")
    block = f"{START_MARKER}\n{section}\n{END_MARKER}"
    if START_MARKER in text and END_MARKER in text:
        text = re.sub(
            re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
            block,
            text,
            count=1,
            flags=re.DOTALL,
        )
    else:
        text = text.rstrip() + "\n\n---\n\n## Open Source Contributions\n\n" + block + "\n"
    README_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    username, specs, _auto_discover = load_config()
    section = build_section(username, specs)
    replace_readme(section)
    print(f"[OK] README.md refreshed for {username}; {len(specs)} repos considered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
