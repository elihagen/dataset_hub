"""Stack versions from GitHub: tags/releases of the robot stack repos.

A dataset's `stack_version` is written as `<repo>@<ref>` (e.g.
`horti_nav_stack@v2.3.6`, or a commit sha), because every stack repo has its
own version numbers. The repos are listed in the config (`github_org`,
`stack_repos`); the hub only reads from the GitHub API.

Auth: $GITHUB_TOKEN, otherwise the token of the GitHub CLI (`gh auth token`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

API = "https://api.github.com"
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def parse_version(value: str, org: str) -> Optional[Tuple[str, str, str]]:
    """'repo@ref' or 'owner/repo@ref' -> (owner, repo, ref); None if not in that form."""
    m = re.fullmatch(r"(?:([\w.-]+)/)?([\w.-]+)@(\S+)", (value or "").strip())
    if not m or not (m.group(1) or org):
        return None
    return m.group(1) or org, m.group(2), m.group(3)


def version_url(value: str, org: str) -> Optional[str]:
    p = parse_version(value, org)
    if p is None:
        return None
    owner, repo, ref = p
    kind = "commit" if SHA_RE.match(ref) else "releases/tag"
    return f"https://github.com/{owner}/{repo}/{kind}/{ref}"


def _token() -> Optional[str]:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


class StackVersions:
    """Tags of the configured repos, cached for `ttl` seconds."""

    def __init__(self, org: str, repos, ttl: float = 600):
        self.org, self.repos, self.ttl = org, list(repos), ttl
        self._cache: Optional[Dict[str, Any]] = None
        self._at = 0.0

    def _get(self, path: str, token: Optional[str]) -> List[Dict[str, Any]]:
        req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def fetch(self) -> Dict[str, Any]:
        """{"versions": [{value, repo, tag, sha, date, url}], "errors": {repo: msg}}, newest first."""
        if self._cache is not None and time.time() - self._at < self.ttl:
            return self._cache
        token = _token()
        versions, errors = [], {}
        for repo in self.repos:
            owner, name = repo.split("/", 1) if "/" in repo else (self.org, repo)
            try:
                tags = self._get(f"/repos/{owner}/{name}/tags?per_page=100", token)
                released = {r["tag_name"]: r.get("published_at")
                            for r in self._get(f"/repos/{owner}/{name}/releases?per_page=100", token)}
            except Exception as e:  # one unreachable repo shouldn't hide the others
                errors[repo] = str(e)
                continue
            for t in tags:
                value = f"{repo}@{t['name']}"
                versions.append({"value": value, "repo": repo, "tag": t["name"],
                                 "sha": t["commit"]["sha"], "date": (released.get(t["name"]) or "")[:10],
                                 "url": version_url(value, self.org)})
        self._cache, self._at = {"versions": versions, "errors": errors}, time.time()
        return self._cache
