"""Core consistency checks between a PyPI release and its GitHub tag.

tagpoint answers one question: does the tag named after the PyPI version
actually point at the code the release was built from?  It fetches the PyPI
metadata, resolves the matching GitHub tag, downloads the sdist, and compares
what can be compared without a full git checkout:

1. the tag exists (v{version} or {version})
2. the sdist PKG-INFO version matches the PyPI version
3. (deep) the sdist file list matches the tag's git tree file list,
   flagging files the tag gained/lost after the release was built
4. (deep) the tag commit date is not newer than the release upload time
   (a tag moved *after* the build is suspicious drift)
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
GITHUB_API = "https://api.github.com"

_USER_AGENT = "tagpoint/0.1.0 (+https://github.com/hahahahahahahahah6/tagpoint)"


class CheckError(Exception):
    """Raised when a check cannot be completed (network, missing data, ...)."""


@dataclass
class Finding:
    ok: bool
    name: str
    detail: str


@dataclass
class Report:
    package: str
    version: str
    repo: str
    tag: str | None
    commit: str | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return bool(self.findings) and all(f.ok for f in self.findings)

    def render(self) -> str:
        lines = [
            f"tagpoint report: {self.package}=={self.version}  repo={self.repo}",
            f"tag: {self.tag or '(none)'}   commit: {(self.commit or '(none)')[:12]}",
            "",
        ]
        for f in self.findings:
            mark = "PASS" if f.ok else "FAIL"
            lines.append(f"[{mark}] {f.name}: {f.detail}")
        lines.append("")
        verdict = "CONSISTENT" if self.consistent else "INCONSISTENT"
        lines.append(f"verdict: {verdict}")
        return "\n".join(lines)


def _fetch_json(url: str, token: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise CheckError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise CheckError(f"network error fetching {url}: {e.reason}") from e


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise CheckError(f"HTTP {e.code} downloading {url}") from e
    except urllib.error.URLError as e:
        raise CheckError(f"network error downloading {url}: {e.reason}") from e


def _sdist_files(blob: bytes, filename: str) -> tuple[str, list[str]]:
    """Return (PKG-INFO version, sorted file list) from an sdist blob."""
    version = ""
    names: list[str] = []
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                names.append(info.filename)
                if info.filename.endswith("PKG-INFO"):
                    version = _pkg_info_version(zf.read(info.filename).decode("utf-8", "replace"))
    else:  # .tar.gz
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for member in tf.getmembers():
                names.append(member.name)
                if member.name.endswith("PKG-INFO") and member.isfile():
                    f = tf.extractfile(member)
                    if f:
                        version = _pkg_info_version(f.read().decode("utf-8", "replace"))
    return version, sorted(names)


def _pkg_info_version(text: str) -> str:
    for line in text.splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_top_dir(names: list[str]) -> set[str]:
    """Drop the sdist's single top-level directory so paths compare to a git tree."""
    out = set()
    for n in names:
        parts = n.strip("/").split("/")
        if len(parts) > 1:
            out.add("/".join(parts[1:]))
    return out


def check(package: str, repo: str, *, deep: bool = True, token: str | None = None) -> Report:
    """Run the full consistency check and return a Report."""
    meta = _fetch_json(PYPI_JSON_URL.format(name=package), token)
    version = meta["info"]["version"]
    report = Report(package=package, version=version, repo=repo, tag=None, commit=None)

    # 1. resolve the tag
    tag_name = None
    commit = None
    for candidate in (f"v{version}", version):
        try:
            ref = _fetch_json(f"{GITHUB_API}/repos/{repo}/git/refs/tags/{candidate}", token)
        except CheckError:
            continue
        tag_name = candidate
        obj = ref["object"]
        if obj["type"] == "tag":  # annotated tag -> peel to commit
            peeled = _fetch_json(obj["url"], token)
            commit = peeled["object"]["sha"]
        else:
            commit = obj["sha"]
        break
    report.tag, report.commit = tag_name, commit
    if tag_name is None:
        report.findings.append(Finding(False, "tag-exists",
                                       f"no tag v{version} or {version} on {repo}"))
        return report
    report.findings.append(Finding(True, "tag-exists",
                                   f"tag {tag_name} -> {commit[:12]}"))

    # 2. sdist sanity: PKG-INFO version must match PyPI version
    files = meta["releases"].get(version, [])
    sdists = [f for f in files if f["packagetype"] == "sdist"]
    if not sdists:
        report.findings.append(Finding(False, "sdist-present",
                                       "no sdist file in this release"))
        return report
    sdist = sdists[0]
    blob = _fetch_bytes(sdist["url"])
    pkg_version, sdist_names = _sdist_files(blob, sdist["filename"])
    if pkg_version == version:
        report.findings.append(Finding(True, "sdist-version",
                                       f"PKG-INFO version {pkg_version} matches PyPI"))
    else:
        report.findings.append(Finding(False, "sdist-version",
                                       f"PKG-INFO says {pkg_version!r}, PyPI says {version!r}"))

    if not deep:
        return report

    # 3. file-list comparison: sdist contents vs tag tree
    tree = _fetch_json(f"{GITHUB_API}/repos/{repo}/git/trees/{commit}?recursive=1", token)
    tag_paths = {t["path"] for t in tree.get("tree", []) if t["type"] == "blob"}
    # PKG-INFO is generated at build time; it never lives in the git tree.
    sdist_paths = _strip_top_dir(sdist_names) - {"PKG-INFO"}
    only_tag = sorted(tag_paths - sdist_paths)
    only_sdist = sorted(sdist_paths - tag_paths)
    if not only_tag and not only_sdist:
        report.findings.append(Finding(True, "file-list",
                                       f"{len(tag_paths)} files match the tag tree"))
    else:
        detail = []
        if only_tag:
            detail.append(f"{len(only_tag)} only in tag (e.g. {', '.join(only_tag[:3])})")
        if only_sdist:
            detail.append(f"{len(only_sdist)} only in sdist (e.g. {', '.join(only_sdist[:3])})")
        report.findings.append(Finding(False, "file-list", "; ".join(detail)))

    # 4. tag commit must not be newer than the release upload
    commit_meta = _fetch_json(f"{GITHUB_API}/repos/{repo}/commits/{commit}", token)
    commit_date = commit_meta["commit"]["committer"]["date"]
    upload_time = sdist["upload_time_iso_8601"]
    if commit_date <= upload_time:
        report.findings.append(Finding(True, "tag-before-build",
                                       f"tag commit {commit_date} <= upload {upload_time}"))
    else:
        report.findings.append(Finding(False, "tag-before-build",
                                       f"tag commit {commit_date} is NEWER than upload {upload_time} — tag drifted"))

    return report
