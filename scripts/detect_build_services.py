#!/usr/bin/env python3
"""Detect which services to build from conventional commit scopes.

Outputs `services=<json-array>` for GITHUB_OUTPUT.

Environment variables (set by the calling workflow):
  GITHUB_EVENT_NAME  - push | pull_request
  COMMITS_JSON       - JSON array of push event commit objects (push only)
  BASE_REF           - base branch for PR events (e.g. main)
  BEFORE_SHA         - push event before SHA (push only)
  AFTER_SHA          - push event after SHA (push only)
"""
import json
import os
import re
import subprocess
from pathlib import Path

REGISTRY_FILE = Path(__file__).parent.parent / "service_registry.yaml"


def known_services() -> set:
    services = set()
    for line in REGISTRY_FILE.read_text().splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent == 2 and stripped.rstrip().endswith(":"):
            services.add(stripped.rstrip()[:-1])
    return services


def service_paths() -> dict:
    """Map service name -> repository path from service_registry.yaml."""
    paths = {}
    current_service = None
    for line in REGISTRY_FILE.read_text().splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent == 2 and stripped.rstrip().endswith(":"):
            current_service = stripped.rstrip()[:-1]
            continue
        if current_service and indent == 4 and stripped.startswith("path:"):
            path_value = stripped.split(":", 1)[1].strip()
            paths[current_service] = path_value.strip('"').strip("'")
    return paths


def scopes_from_messages(messages: list) -> set:
    """Extract scopes from conventional commit messages.

    Handles both regular commits and merge/squash commits whose bodies
    contain bullet lines like '* feat(auth): ...' or '- fix(device,salt): ...'
    """
    scopes = set()
    conv_re = re.compile(r"(?:^|^[*\-]\s*)\w+\(([^)]+)\)")
    for msg in messages:
        for line in msg.splitlines() if msg else []:
            line = line.strip()
            m = conv_re.match(line)
            if m:
                for s in m.group(1).split(","):
                    scopes.add(s.strip())
    return scopes


def commit_messages() -> list:
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        raw = os.environ.get("COMMITS_JSON") or "[]"
        try:
            commits = json.loads(raw)
            if not isinstance(commits, list):
                return []
            return [c.get("message", "") for c in commits if isinstance(c, dict)]
        except json.JSONDecodeError:
            return []
    # pull_request and other events: use git log
    base = os.environ.get("BASE_REF", "main")
    result = subprocess.run(
        ["git", "log", f"origin/{base}..HEAD", "--format=%s"],
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def changed_files() -> list:
    """Return changed file paths for push/PR events."""
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        raw = os.environ.get("COMMITS_JSON") or "[]"
        try:
            commits = json.loads(raw)
            if not isinstance(commits, list):
                return []
            files = []
            for commit in commits:
                if not isinstance(commit, dict):
                    continue
                files.extend(commit.get("added", []))
                files.extend(commit.get("modified", []))
                files.extend(commit.get("removed", []))
            if files:
                return files
        except json.JSONDecodeError:
            pass

        # Some push payloads omit per-commit file lists. Fall back to git diff.
        before_sha = os.environ.get("BEFORE_SHA", "").strip()
        after_sha = os.environ.get("AFTER_SHA", "").strip()
        if before_sha and after_sha and before_sha != "0000000000000000000000000000000000000000":
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{before_sha}..{after_sha}"],
                capture_output=True,
                text=True,
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return []

    base = os.environ.get("BASE_REF", "main")
    result = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base}..HEAD"],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def services_from_paths(files: list, svc_paths: dict) -> set:
    """Infer affected services from changed file paths.

    A service with path '.' is treated as a root-level service: any file
    change triggers a build for that service.
    """
    matched = set()
    normalized = [p.lstrip("./") for p in files]
    for svc, svc_path in svc_paths.items():
        clean_path = svc_path.strip("/")
        if not clean_path or clean_path == ".":
            # Root-level service — any change triggers a build.
            if files:
                matched.add(svc)
            continue
        prefix = clean_path + "/"
        for file_path in normalized:
            if file_path == clean_path or file_path.startswith(prefix):
                matched.add(svc)
                break
    return matched


def main():
    messages = commit_messages()
    scopes = scopes_from_messages(messages)
    known = known_services()
    services = known & scopes

    # Fallback: if commit messages do not include scopes, infer from changed files.
    if not services:
        files = changed_files()
        services = services_from_paths(files, service_paths())

    services = sorted(services)
    print(f"services={json.dumps(services)}")


if __name__ == "__main__":
    main()
