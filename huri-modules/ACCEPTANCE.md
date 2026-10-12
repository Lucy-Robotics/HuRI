# Listing a module in huri-modules

Being listed in `registry.toml` is HuRI's "verified" mark (tier 3). It is a
trust label, not a gate: any module can be used without it (tiers 0–2). A
listing is a pull request adding one `[[module]]` entry (and, once modules live
in their own repositories, the submodule pinned at a tag).

## What a listing requires

CI (`check_registry.py --run-tests`) checks the mechanical part:

- the package installs and provides the module under the listed name;
- it ships a `huri_module.toml` whose `summary` and `huri_core` match the entry;
- HuRI's startup checks pass against the current `huri-core` and events: its
  topics are registered, with the event classes it declares, and the installed
  versions are in its manifest's ranges;
- every topic it produces belongs to an event package its manifest declares;
- its own tests pass (with `huri_core.testing`, no Ray, no GPU).

A maintainer reviews the rest:

- **License**: OSI-approved and compatible with HuRI's GPL-3.0.
- **Scope**: one clear job, described by its summary; no network access or
  side effects beyond what the summary says.
- **Namespace**: the topics it introduces use its own `<name>.` namespace,
  never `huri.` (reserved for `huri-events`).
- **Actuation**: a module that can move hardware declares
  `requires.actuation = true` and documents its limits; the server will not
  load it without an explicit operator opt-in.
- **Maintenance**: a named maintainer and an issue tracker.

Reviewers are the HuRI maintainers (admins of the repository). Merging the PR is
the verification; there is no separate approval step.

## Keeping it true

- Bumping a listed module is a pull request moving its pin; CI runs again.
- When `huri-core` publishes a new minor version, every listed module is
  re-checked against it (CI on the release PR); entries that fail are fixed or
  delisted in the same release.
- A verified mark is revoked (the entry removed) when the module stops passing
  CI and its maintainer does not respond within a release cycle, or on a
  security or safety problem, immediately.
