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
Before `0.X.0` is tagged, the lower bound is `0.X.0.dev0` (`>=0.4.0.dev0,<0.5`
today): a `.devN` build is a pre-release below `0.X`, which `>=0.X` excludes.

Public surface, i.e. what a breaking change is measured against:

- the base classes modules and events derive from: `Module`,
  `ModuleWithHandle`, `ModuleWithId`, `EventData`, `JsonEvent`, `BytesEvent`;
- the module test harness: `huri_core.testing` (`ModuleHarness`,
  `LocalHandle`);
- the client SDK and the client/server config schema (YAML keys);
- the wire protocol: session handshake, JSON and binary framing, topic
  namespaces, and the server's HTTP API (the `GET /modules` response);
- the plugin entry point groups (`huri.modules`, `huri.events`,
  `huri.commands`), the `HURI_MODULES` / `HURI_ACTUATION_MODULES` settings and
  the module manifest (`huri_module.toml`);
- the `huri` command: how it dispatches to `huri.commands` entry points and
  the signature it calls them with (`main(argv: list[str]) -> int`).

`huri hub` and the registry schema it reads are not huri-core's: both live in
huri-modules (the `huri-hub` package) and follow its releases.

Everything else (bus internals, factories, server wiring) is private and may
change in any release.

## Who releases, and how

Repository maintainers (admins of the `Lucy-Robotics/huri-core` repository)
cut releases:

1. Open a release PR on `dev` that sets `__version__` to the release version
   (drop the `.devN` suffix) and summarizes the changes. On a minor release,
   it also turns the documented ranges (README's examples and Versioning
   section) from `>=0.X.0.dev0,<0.(X+1)` into `>=0.X,<0.(X+1)`.
2. Merge it, then merge `dev` into `main` with a PR named `Release X.Y.Z`.
3. Tag `main`'s merge commit `X.Y.Z` (no `v` prefix, like `0.2.0` and `0.3.0`)
   and push the tag. Publish a GitHub release for it: its notes are the
   changelog, and they say what breaks for modules on a minor release.
4. On `dev`, bump `__version__` to the next `X.(Y+1).0.dev0`.

This repository was renamed from `Lucy-Robotics/HuRI` to
`Lucy-Robotics/huri-core` at the split. The name `HuRI` is never reused (the
showcase lives at
[Lucy-Robotics/HuRI-landing](https://github.com/Lucy-Robotics/HuRI-landing)),
so GitHub keeps redirecting old clones, remotes and
`git+https://github.com/Lucy-Robotics/HuRI.git` install strings to huri-core.
New install strings, docs and git remotes must still use
`Lucy-Robotics/huri-core`. The earlier tags came with the history; the first
release as `huri-core` is `0.4.0`.

## Publishing

Releases are git-only, there is no PyPI package to maintain:

```bash
pip install "huri-core[server] @ git+https://github.com/Lucy-Robotics/huri-core.git@0.4.0"
python -c "import huri_core; print(huri_core.__version__)"
```

`[client]` and `[dev]` install the same way.

## huri-core and huri-modules

[huri-modules](https://github.com/Lucy-Robotics/huri-modules) (the curated
events, the curated modules, the registry and the install/deploy tooling) has
its own semver and its own tags, also `X.Y.Z`. Each of its releases is a
tested set built against one huri-core ref, `HURI_CORE_REF` in its
`versions.env` file; its packages pin a huri-core minor range.

| huri-modules | huri-core |
| ------------ | --------- |
| 0.1.x        | 0.4.x     |

Once `0.4.0` is tagged here, huri-modules moves `HURI_CORE_REF` from `dev` to
`0.4.0` and its packages' range from `>=0.4.0.dev0,<0.5` to `>=0.4,<0.5`.

### How a breaking change propagates

1. A change here breaks the public surface: it lands on `dev` and the next
   release is a **minor** bump (`0.4` → `0.5`), with the break described in the
   release notes.
2. huri-modules' CI runs its tests against huri-core `dev` as a non-blocking
   canary on every push, next to the blocking run against its pinned
   `HURI_CORE_REF`. The canary turns red: the break is visible there before
   anything is released, and before it reaches a user.
3. Once huri-core `0.5.0` is tagged, huri-modules ports its packages, moves
   their range (`huri-core>=0.5,<0.6`, bumping each package's own version),
   points `HURI_CORE_REF` at `0.5.0`, and adds a row to the compatibility
   table.
4. huri-modules releases a new minor (`0.2.0`), the set that works with
   huri-core `0.5.x`. Users of huri-core `0.4.x` keep using huri-modules
   `0.1.x` (`HURI_MODULES_REF=0.1.0` for `huri hub`), whose ranges keep pip
   from pairing it with the wrong core.

Clients built on the SDK are not part of that set.
[huri-cli](https://github.com/Lucy-Robotics/huri-cli), the reference client,
has its own versions and tags; its CI runs against huri-core at its
`HURI_CORE_REF` (default `dev`), and it moves its `huri-core[client]` range
the same way once huri-core is tagged. huri-modules' installer pins the
huri-cli ref it installs in `versions.env` (`HURI_CLI_REF`).

Third-party modules follow the same path at their own pace: their
`huri_core` range in `huri_module.toml` tells the server (at startup) and
`huri hub` (when listing and installing) which huri-core they support.
