#!/usr/bin/env python3

from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "dependencies.yaml"
README_PATH = ROOT / "README.md"

SITE_DIR = ROOT / "site"
SITE_PATH = SITE_DIR / "index.html"

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

def fetch_dependencies(config: dict):
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

    return github_actions, helm_charts, terraform_providers

def build_dashboard(
    github_actions,
    helm_charts,
    terraform_providers,
) -> str:
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

def html_table(rows: list[dict[str, str]]) -> str:
    table_rows = []

    for row in rows:
        name = html.escape(row["name"])
        major = html.escape(row["major"])
        latest = html.escape(row["latest"])
        released = html.escape(row["released"])
        url = html.escape(row["url"], quote=True)

        table_rows.append(
            f"""
            <tr>
                <td>
                    <a href="{url}" target="_blank" rel="noopener noreferrer">
                        {name}
                    </a>
                </td>
                <td><span class="major">{major}</span></td>
                <td>
                    <a href="{url}" target="_blank" rel="noopener noreferrer">
                        {latest}
                    </a>
                </td>
                <td>{released}</td>
            </tr>
            """
        )

    return "\n".join(table_rows)


def build_site(
    github_actions: list[dict[str, str]],
    helm_charts: list[dict[str, str]],
    terraform_providers: list[dict[str, str]],
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Dependency Tracker</title>

    <style>
        :root {{
            color-scheme: light dark;
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;
        }}

        body {{
            margin: 0;
            background: #0d1117;
            color: #e6edf3;
        }}

        main {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 48px 24px 80px;
        }}

        header {{
            margin-bottom: 48px;
        }}

        h1 {{
            margin-bottom: 8px;
            font-size: 2.4rem;
        }}

        header p {{
            margin: 0;
            color: #8b949e;
            font-size: 1.05rem;
        }}

        section {{
            margin-top: 42px;
        }}

        h2 {{
            margin-bottom: 16px;
            font-size: 1.4rem;
        }}

        .table-wrapper {{
            overflow-x: auto;
            border: 1px solid #30363d;
            border-radius: 8px;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            background: #161b22;
        }}

        th,
        td {{
            padding: 12px 16px;
            text-align: left;
            border-bottom: 1px solid #30363d;
        }}

        th {{
            color: #8b949e;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}

        tbody tr:last-child td {{
            border-bottom: 0;
        }}

        tbody tr:hover {{
            background: #1c2128;
        }}

        a {{
            color: #58a6ff;
            text-decoration: none;
        }}

        a:hover {{
            text-decoration: underline;
        }}

        .major {{
            display: inline-block;
            padding: 3px 8px;
            border: 1px solid #3fb950;
            border-radius: 999px;
            color: #3fb950;
            font-weight: 600;
        }}

        footer {{
            margin-top: 48px;
            color: #8b949e;
            font-size: 0.85rem;
        }}

        @media (prefers-color-scheme: light) {{
            body {{
                background: #ffffff;
                color: #1f2328;
            }}

            table {{
                background: #ffffff;
            }}

            .table-wrapper,
            th,
            td {{
                border-color: #d0d7de;
            }}

            tbody tr:hover {{
                background: #f6f8fa;
            }}

            header p,
            th,
            footer {{
                color: #656d76;
            }}

            a {{
                color: #0969da;
            }}

            .major {{
                color: #1a7f37;
                border-color: #1a7f37;
            }}
        }}
    </style>
</head>

<body>
    <main>
        <header>
            <h1>Dependency Tracker</h1>
            <p>
                Latest versions of the GitHub Actions, Helm charts,
                and Terraform providers I care about.
            </p>
        </header>

        <section>
            <h2>GitHub Actions</h2>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Dependency</th>
                            <th>Major</th>
                            <th>Latest</th>
                            <th>Released</th>
                        </tr>
                    </thead>
                    <tbody>
                        {html_table(github_actions)}
                    </tbody>
                </table>
            </div>
        </section>

        <section>
            <h2>Helm Charts</h2>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Dependency</th>
                            <th>Major</th>
                            <th>Latest</th>
                            <th>Released</th>
                        </tr>
                    </thead>
                    <tbody>
                        {html_table(helm_charts)}
                    </tbody>
                </table>
            </div>
        </section>

        <section>
            <h2>Terraform Providers</h2>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Dependency</th>
                            <th>Major</th>
                            <th>Latest</th>
                            <th>Released</th>
                        </tr>
                    </thead>
                    <tbody>
                        {html_table(terraform_providers)}
                    </tbody>
                </table>
            </div>
        </section>

        <footer>
            Generated automatically from dependencies.yaml.
        </footer>
    </main>
</body>
</html>
"""


def write_site(
    github_actions: list[dict[str, str]],
    helm_charts: list[dict[str, str]],
    terraform_providers: list[dict[str, str]],
) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    site = build_site(
        github_actions,
        helm_charts,
        terraform_providers,
    )

    SITE_PATH.write_text(site)

def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text())

    (
        github_actions,
        helm_charts,
        terraform_providers,
    ) = fetch_dependencies(config)

    dashboard = build_dashboard(
        github_actions,
        helm_charts,
        terraform_providers,
    )

    update_readme(dashboard)

    write_site(
        github_actions,
        helm_charts,
        terraform_providers,
    )

    print(f"Updated {README_PATH}")
    print(f"Updated {SITE_PATH}")

if __name__ == "__main__":
    main()