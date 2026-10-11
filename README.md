# tagpoint

Verify that a PyPI release **actually matches** its GitHub tag — the name *and* the content.

```bash
pip install tagpoint
tagpoint check requests --repo psf/requests
```

Output:

```
tagpoint report: requests==2.32.3  repo=psf/requests
tag: v2.32.3   commit: abc123def456

[PASS] tag-exists: tag v2.32.3 -> abc123def456
[PASS] sdist-version: PKG-INFO version 2.32.3 matches PyPI
[PASS] file-list: 128 files match the tag tree
[PASS] tag-before-build: tag commit 2024-05-29T... <= upload 2024-05-29T...

verdict: CONSISTENT
```

Exit code `0` = consistent, `1` = inconsistent, `2` = check error (network, missing data).

## Install

```bash
pip install tagpoint
```

tagpoint is stdlib-only — no third-party dependencies.

## Usage

```
tagpoint check <pypi-package> --repo owner/name [--shallow] [--token TOKEN]
```

- `tagpoint check flask --repo pallets/flask` — full check (default)
- `--shallow` — only verify the tag exists and the sdist version matches
- `--token` — GitHub token (or `GITHUB_TOKEN` env var) to raise API rate limits

Four checks run by default:

1. **tag-exists** — a tag named `v{version}` or `{version}` exists and resolves (annotated tags are peeled to the commit).
2. **sdist-version** — the sdist's `PKG-INFO` version equals the PyPI version.
3. **file-list** — the sdist file list matches the tag's git tree (catches files added/removed after the build).
4. **tag-before-build** — the tag commit date is not newer than the release upload time (a tag moved *after* the build is drift).

## Differentiation

There are tools that check whether a release tag **exists** (e.g. my
[tagtruth](https://github.com/hahahahahahahahah6/tagtruth) project checks tag
presence as part of release hygiene). tagpoint goes one step further: it checks
whether the tag **points at the code the release was actually built from**.

The failure mode it catches is real: a tag named `v1.2.3` exists, CI is green,
everything looks fine — but the tag was force-moved after the sdist was built,
or the release was cut from a dirty working tree. The name matches; the
content doesn't. tagpoint compares the sdist contents against the tag's git
tree and flags the drift. Think of it as a "name matches reality" check for
your supply chain.

## License

MIT — see [LICENSE](LICENSE).
