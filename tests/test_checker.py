"""Smoke tests for tagpoint. All network access is mocked — no real HTTP."""

from __future__ import annotations

import io
import json
import tarfile
import urllib.error

import pytest

from tagpoint import checker
from tagpoint.checker import CheckError, check

PKG = "fakepkg"
REPO = "owner/fakepkg"
VERSION = "1.2.3"


def make_sdist(version: str, files: tuple[str, ...] = ("fakepkg/__init__.py",)) -> bytes:
    buf = io.BytesIO()
    top = f"fakepkg-{version}"
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        pkg_info = f"Metadata-Version: 2.1\nName: fakepkg\nVersion: {version}\n".encode()
        for name in (f"{top}/PKG-INFO",) + tuple(f"{top}/{f}" for f in files):
            data = pkg_info if name.endswith("PKG-INFO") else b"x = 1\n"
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def pypi_meta(version=VERSION, with_sdist=True):
    files = []
    if with_sdist:
        files.append({
            "packagetype": "sdist",
            "filename": f"fakepkg-{version}.tar.gz",
            "url": f"https://files.pythonhosted.org/fakepkg-{version}.tar.gz",
            "upload_time_iso_8601": "2026-10-01T12:00:00Z",
        })
    return {"info": {"version": version}, "releases": {version: files}}


class FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Router:
    """Maps URL substrings to payloads for urlopen."""

    def __init__(self):
        self.routes: list[tuple[str, bytes]] = []

    def add(self, needle: str, payload):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        self.routes.append((needle, payload))

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        for needle, payload in self.routes:
            if needle in url:
                return FakeResp(payload)
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


@pytest.fixture
def net(monkeypatch):
    router = Router()
    monkeypatch.setattr(checker.urllib.request, "urlopen", router)
    return router


def wire_happy(router: Router, version=VERSION, tag_commit_date="2026-09-30T12:00:00Z",
               tree_files=("fakepkg/__init__.py",), sdist_files=("fakepkg/__init__.py",),
               sdist_version=None):
    sdist_version = sdist_version or version
    router.add("pypi.org", pypi_meta(version))
    router.add("git/refs/tags/v", {"object": {"type": "commit",
                                              "sha": "abc123" + "0" * 34,
                                              "url": "x"}})
    router.add("git/trees", {"tree": [{"path": p, "type": "blob"} for p in tree_files]})
    router.add("repos/owner/fakepkg/commits",
               {"commit": {"committer": {"date": tag_commit_date}}})
    router.add("files.pythonhosted.org", make_sdist(sdist_version, sdist_files))


def test_happy_path_consistent(net):
    wire_happy(net)
    r = check(PKG, REPO)
    assert r.consistent
    assert r.version == VERSION
    assert r.tag == f"v{VERSION}"
    assert len(r.findings) == 4
    out = r.render()
    assert "CONSISTENT" in out
    assert "[PASS]" in out and "[FAIL]" not in out


def test_missing_tag_is_inconsistent(net):
    net.add("pypi.org", pypi_meta())
    # no tag routes -> 404 on both v{version} and {version}
    r = check(PKG, REPO)
    assert not r.consistent
    assert r.tag is None
    assert "INCONSISTENT" in r.render()


def test_sdist_version_mismatch(net):
    wire_happy(net, sdist_version="9.9.9")
    r = check(PKG, REPO)
    assert not r.consistent
    assert any(f.name == "sdist-version" and not f.ok for f in r.findings)


def test_no_sdist_in_release(net):
    net.add("pypi.org", pypi_meta(with_sdist=False))
    net.add("git/refs/tags/v", {"object": {"type": "commit", "sha": "a" * 40, "url": "x"}})
    r = check(PKG, REPO)
    assert not r.consistent
    assert any(f.name == "sdist-present" and not f.ok for f in r.findings)


def test_file_list_drift_detected(net):
    # tag gained a file after the release was built
    wire_happy(net, tree_files=("fakepkg/__init__.py", "fakepkg/backdoor.py"))
    r = check(PKG, REPO)
    assert not r.consistent
    bad = [f for f in r.findings if f.name == "file-list"]
    assert bad and not bad[0].ok
    assert "backdoor.py" in bad[0].detail


def test_tag_newer_than_upload_detected(net):
    wire_happy(net, tag_commit_date="2026-10-05T12:00:00Z")  # after upload 10-01
    r = check(PKG, REPO)
    assert not r.consistent
    bad = [f for f in r.findings if f.name == "tag-before-build"]
    assert bad and not bad[0].ok


def test_annotated_tag_is_peeled(net):
    net.add("pypi.org", pypi_meta())
    net.add("git/refs/tags/v",
            {"object": {"type": "tag", "sha": "t" * 40, "url": "https://api.github.com/peeled"}})
    net.add("peeled", {"object": {"type": "commit", "sha": "c" * 40}})
    net.add("git/trees", {"tree": [{"path": "fakepkg/__init__.py", "type": "blob"}]})
    net.add("repos/owner/fakepkg/commits",
            {"commit": {"committer": {"date": "2026-09-30T12:00:00Z"}}})
    net.add("files.pythonhosted.org", make_sdist(VERSION))
    r = check(PKG, REPO)
    assert r.consistent
    assert r.commit == "c" * 40


def test_pypi_404_raises_check_error(net):
    # Router raises HTTPError 404 -> CheckError
    with pytest.raises(CheckError):
        check("no-such-pkg", REPO)


def test_shallow_skips_deep_checks(net):
    wire_happy(net)
    r = check(PKG, REPO, deep=False)
    assert r.consistent
    assert [f.name for f in r.findings] == ["tag-exists", "sdist-version"]


def test_cli_exit_codes(net, capsys, monkeypatch):
    from tagpoint import cli
    wire_happy(net)
    monkeypatch.setattr(checker.urllib.request, "urlopen", net)
    assert cli.main(["check", PKG, "--repo", REPO]) == 0
    out, _ = capsys.readouterr()
    assert "CONSISTENT" in out

    # drift case -> exit 1
    net2 = Router()
    wire_happy(net2, tree_files=("fakepkg/__init__.py", "fakepkg/evil.py"))
    monkeypatch.setattr(checker.urllib.request, "urlopen", net2)
    assert cli.main(["check", PKG, "--repo", REPO]) == 1
