# Contributing to HuRI

HuRI spans several repositories (see [HuRI ecosystem](README.md#huri-ecosystem)),
plus the use cases built on it; pick the line that matches what you want to
change, and open the issue or pull request there.

| You want to… | Repository | How |
|---|---|---|
| Add a module (a new capability) | your own, created from [huri-plugin-template](https://github.com/Lucy-Robotics/huri-plugin-template) | nobody's permission needed: publish it, tag it `huri-module` to be found by `huri hub search`; for the verified mark, open a pull request to [huri-modules](https://github.com/Lucy-Robotics/huri-modules) adding it to `registry.toml` and pinning it as a submodule under `modules/<name>` ([ACCEPTANCE.md](https://github.com/Lucy-Robotics/huri-modules/blob/main/ACCEPTANCE.md)) |
| Add an event type | an events package of your own, in your own topic namespace (`acme.*`) | see the template's `events/`; `huri.*` is reserved for [huri-events](https://github.com/Lucy-Robotics/huri-modules/tree/main/packages/huri-events) |
| Fix or improve a curated module, or `huri-events` | [huri-modules](https://github.com/Lucy-Robotics/huri-modules) ([issues](https://github.com/Lucy-Robotics/huri-modules/issues)) | pull request on `dev` to that package under `packages/`, with tests (`huri_core.testing`); bump the package's version |
| Change the hub (`huri hub`), the registry's format or its checker | [huri-modules](https://github.com/Lucy-Robotics/huri-modules) ([issues](https://github.com/Lucy-Robotics/huri-modules/issues)) | pull request on `dev` to `packages/huri-hub` (the registry parser lives there; `check_registry.py` reuses it), with tests; bump the package's version |
| Change install or deployment | [huri-modules](https://github.com/Lucy-Robotics/huri-modules) | pull request on `dev`; CI plans the installer on three distros (`ci.yml`); a change to `deploy/` or `packages/` also builds the base image and runs a session against it (`docker.yml`); the GPU images and a real install run on demand (`docker.yml`'s `gpu_images`, `install.yml`) |
| Change the framework (bus, server, SDK, loader, the `huri` command dispatcher) | [huri-core](https://github.com/Lucy-Robotics/huri-core) ([issues](https://github.com/Lucy-Robotics/huri-core/issues)) | pull request on `dev`; mind the public surface and semver ([RELEASING.md](RELEASING.md)) |
| Fix or improve the reference client (`huri-cli`), its example configs or the `huri` metapackage | [huri-cli](https://github.com/Lucy-Robotics/huri-cli) ([issues](https://github.com/Lucy-Robotics/huri-cli/issues)) | pull request on `dev`, with tests; CI runs pytest and lint on Python 3.10–3.12 against `huri-core` and `huri-modules` `dev` |
| Improve the module starter | [huri-plugin-template](https://github.com/Lucy-Robotics/huri-plugin-template) | pull request on `main` |
| Improve the HuRI-wide pages (the README's ecosystem section, this file, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)) | [huri-core](https://github.com/Lucy-Robotics/huri-core) | pull request on `dev`; CI runs `python scripts/check_links.py` |
| Fix or improve a use case (InMoov, the acceptance test site, ...) | its own repository, e.g. [HuRI-inmoov](https://github.com/Lucy-Robotics/HuRI-inmoov) | that repository's own process |

The pages of huri-core (README.md, CONTRIBUTING.md, RELEASING.md, docs/)
link into `huri-modules` and `huri-cli` at `main`. `python
scripts/check_links.py` checks every relative link and anchor, and that each
`Lucy-Robotics` URL names a known repository; with `--siblings DIR` it also
resolves each deep link (`tree/<ref>/<path>`, `#anchor`, `#subdirectory=`)
against checkouts in `DIR/<repo>`.

Not sure where something belongs? Open an issue on
[huri-core](https://github.com/Lucy-Robotics/huri-core/issues) and it will be
moved.

In `huri-core` and `huri-modules`, run `make check` locally first. CI runs the
same lint (black, isort, flake8, mypy) and pytest as separate steps, not
`make check` itself; in `huri-modules`, mypy gates only the files that are
already clean.

## Versioning, in short

- `huri-core`, `huri-modules` and `huri-cli` each follow semver, with tags
  `X.Y.Z` (no `v`) on `main` and work on `dev`. While `0.x`, a minor bump may
  break the public surface; a patch must not.
- A change to `huri-core`'s public surface is a minor bump; `huri-modules`
  sees it first in its non-blocking canary CI (against `huri-core` `dev`), then
  adopts it by bumping `HURI_CORE_REF` in `versions.env` in a pull request.
- In `huri-modules`, each package has its own version in its `pyproject.toml`:
  bump it when the package changes. A wire-format change to `huri-events` is a
  minor bump while `0.x`.
- Working on `huri-core` and `huri-modules` together: set
  `HURI_CORE_PATH=/path/to/huri-core` and `huri-modules` installs that checkout
  (editable) instead of `HURI_CORE_REF`; likewise `HURI_CLI_PATH` for a
  `huri-cli` checkout instead of `HURI_CLI_REF`.

Details: [huri-core's RELEASING.md](RELEASING.md),
[huri-modules' RELEASING.md](https://github.com/Lucy-Robotics/huri-modules/blob/main/RELEASING.md),
and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#versions-and-how-a-core-change-propagates).
