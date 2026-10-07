#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "dependencies.yaml"
README_PATH = ROOT / "README.md"

START_MARKER = "<!-- dependency-tracker:start -->"
END_MARKER = "<!-- dependency-tracker:end -->"

GITHUB_API = "https://api.github.com"
TERRAFORM_REGISTRY = "https://registry.terraform.io"


def request_text(url: str, headers: dict[str, str] | None = None) -> str:
    request_headers = {
        "User-Agent": "dependency-tracker",
        **(headers or {}),
    }

    request = urllib.request.Request(url, headers=request_headers)

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def request_json(url: str, headers: dict[str, str] | None = None):
    return json.loads(request_text(url, headers))


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def normalize_version(version: str) -> str:
    return version.lstrip("v")


def version_key(version: str):
    """
    Convert a version like:
      v8.1.2
      8.1.2
      8.1.2-beta.1

    into something sortable.

    Prerelease suffixes are intentionally ignored for ordering.
    """
    normalized = normalize_version(version)
    numeric = normalized.split("-", 1)[0]

    parts = []

    for part in numeric.split("."):
        match = re.match(r"(\d+)", part)
        parts.append(int(match.group(1)) if match else 0)

    while len(parts) < 3:
        parts.append(0)

    return tuple(parts)


def major_version(version: str, prefix: str = "") -> str:
    match = re.match(r"v?(\d+)", version)

    if not match:
        return "—"

    return f"{prefix}{match.group(1)}"


def format_date(value: str | None) -> str:
    if not value:
        return "—"

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        return value


def get_github_action(action: str) -> dict[str, str]:
    url = f"{GITHUB_API}/repos/{action}/releases/latest"

    try:
        release = request_json(url, github_headers())
        version = release["tag_name"]

        return {
            "name": action,
            "major": major_version(version, "v"),
            "latest": version,
            "released": format_date(release.get("published_at")),
            "url": release["html_url"],
        }

    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise

    # Some actions use tags but don't create GitHub Releases.
    tags = request_json(
        f"{GITHUB_API}/repos/{action}/tags?per_page=100",
        github_headers(),
    )

    if not tags:
        raise RuntimeError(f"No releases or tags found for {action}")

    versions = [
        tag["name"]
        for tag in tags
        if re.match(r"^v?\d+(?:\.\d+)*", tag["name"])
    ]

    if not versions:
        raise RuntimeError(f"No version-like tags found for {action}")

    version = max(versions, key=version_key)

    return {
        "name": action,
        "major": major_version(version, "v"),
        "latest": version,
        "released": "—",
        "url": f"https://github.com/{action}/releases/tag/{version}",
    }


def get_helm_chart(chart: dict[str, str]) -> dict[str, str]:
    name = chart["name"]
    repository = chart["repository"].rstrip("/")

    index = yaml.safe_load(request_text(f"{repository}/index.yaml"))

    entries = index.get("entries", {}).get(name, [])

    if not entries:
        raise RuntimeError(
            f"Chart {name!r} was not found in {repository}/index.yaml"
        )

    stable_entries = [
        entry
        for entry in entries
        if "-" not in str(entry.get("version", ""))
    ]

    candidates = stable_entries or entries
    latest = max(candidates, key=lambda entry: version_key(entry["version"]))

    version = latest["version"]

    return {
        "name": name,
        "major": major_version(version),
        "latest": version,
        "released": format_date(latest.get("created")),
        "url": repository,
    }


def get_terraform_provider(provider: str) -> dict[str, str]:
    namespace, name = provider.split("/", 1)

    data = request_json(
        f"{TERRAFORM_REGISTRY}/v1/providers/{namespace}/{name}/versions"
    )

    versions = [
        item["version"]
        for item in data.get("versions", [])
        if "-" not in item["version"]
    ]

    if not versions:
        raise RuntimeError(f"No stable versions found for {provider}")

    version = max(versions, key=version_key)

    return {
        "name": provider,
        "major": major_version(version),
        "latest": version,
        "released": "—",
        "url": (
            f"https://registry.terraform.io/providers/"
            f"{namespace}/{name}/latest"
        ),
    }


def markdown_table(rows: list[dict[str, str]]) -> str:
    output = [
        "| Dependency | Major | Latest | Released |",
        "|---|---:|---:|---:|",
    ]

    for row in rows:
        output.append(
            f"| [{row['name']}]({row['url']}) "
            f"| **{row['major']}** "
            f"| [{row['latest']}]({row['url']}) "
            f"| {row['released']} |"
        )

    return "\n".join(output)


def safe_fetch(label: str, function, item):
    try:
        return function(item)

    except Exception as error:
        print(f"WARNING: Could not fetch {label}: {error}", file=sys.stderr)

        name = item if isinstance(item, str) else item.get("name", "unknown")

        return {
            "name": name,
            "major": "—",
            "latest": "Error",
            "released": "—",
            "url": "#",
        }


def build_dashboard(config: dict) -> str:
    github_actions = [
        safe_fetch(action, get_github_action, action)
        for action in config.get("github_actions", [])
    ]

    helm_charts = [
        safe_fetch(chart["name"], get_helm_chart, chart)
        for chart in config.get("helm_charts", [])
    ]

    terraform_providers = [
        safe_fetch(provider, get_terraform_provider, provider)
        for provider in config.get("terraform_providers", [])
    ]

    sections = [
        START_MARKER,
        "",
        "## GitHub Actions",
        "",
        markdown_table(github_actions),
        "",
        "## Helm Charts",
        "",
        markdown_table(helm_charts),
        "",
        "## Terraform Providers",
        "",
        markdown_table(terraform_providers),
        "",
        END_MARKER,
    ]

    return "\n".join(sections)


def update_readme(dashboard: str) -> None:
    if README_PATH.exists():
        current = README_PATH.read_text()
    else:
        current = "# Dependency Tracker\n\n"

    if START_MARKER in current and END_MARKER in current:
        pattern = re.compile(
            re.escape(START_MARKER)
            + r".*?"
            + re.escape(END_MARKER),
            re.DOTALL,
        )

        updated = pattern.sub(dashboard, current)

    else:
        updated = current.rstrip() + "\n\n" + dashboard + "\n"

    README_PATH.write_text(updated)


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text())

    dashboard = build_dashboard(config)
    update_readme(dashboard)

    print(f"Updated {README_PATH}")


if __name__ == "__main__":
    main()