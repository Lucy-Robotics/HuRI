# Releasing huri-core

## Version

The version lives in one place: `__version__` in `huri_core/__init__.py`.
`pyproject.toml` reads it (`[tool.setuptools.dynamic]`), so an installed
distribution and `huri_core.__version__` always agree.

Between releases `dev` carries the next version with a `.devN` suffix
(`0.4.0.dev0`). Git tags are not used to compute the version: releases are
tagged on `main`'s merge commit, which `dev` never contains, so a tag-derived
version would be wrong on `dev`.

## Semver policy

`huri-core` follows semver. While the major version is `0`, a **minor** bump
(`0.3` → `0.4`) may break the public surface and a **patch** bump must not.
Third-party modules and event packages therefore pin `huri-core>=0.X,<0.(X+1)`.

Public surface, i.e. what a breaking change is measured against:

- the base classes modules and events derive from: `Module`,
  `ModuleWithHandle`, `ModuleWithId`, `EventData`, `JsonEvent`, `BytesEvent`;
- the client SDK and the client/server config schema (YAML keys);
- the wire protocol: session handshake, JSON and binary framing, topic names;
- the plugin entry point groups and the module manifest, once they exist.

Everything else (bus internals, factories, server wiring) is private and may
change in any release.

## Who releases, and how

Repository maintainers (admins of the `Lucy-Robotics/HuRI` repository) cut
releases:

1. Open a release PR on `dev` that sets `__version__` to the release version
   (drop the `.devN` suffix) and summarizes the changes.
2. Merge it, then merge `dev` into `main` with a PR named `Release X.Y.Z`.
3. Tag `main`'s merge commit `X.Y.Z` (no `v` prefix, like `0.2.0` and `0.3.0`)
   and push the tag.
4. On `dev`, bump `__version__` to the next `X.(Y+1).0.dev0`.

## Publishing

Releases are git-only, there is no PyPI package to maintain:

```bash
pip install "git+https://github.com/Lucy-Robotics/HuRI.git@0.4.0"
python -c "import huri_core; print(huri_core.__version__)"
```
