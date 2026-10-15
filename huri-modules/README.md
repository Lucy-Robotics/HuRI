# huri-modules

The official, PR-reviewed set of curated HuRI modules — the "verified" index
(tier 3) — and, eventually, the one repository to clone to run HuRI.

| File | What it is |
|---|---|
| `registry.toml` | every curated module (and event package): name, package, summary, supported `huri-core` range, when it was verified, where its reviewed code is |
| `check_registry.py` | the CI check of every entry against its package (`--run-tests` also runs each package's tests) |
| `ACCEPTANCE.md` | what a listing requires, who reviews it, how it is kept true |
| `install.sh` | installs and configures the listed modules on this machine |

```sh
python huri-modules/check_registry.py --run-tests
huri-modules/install.sh --plan-only
```

## From this directory to its own repository

Today this directory lives in the HuRI repository and every `source` is a
`path` into `packages/`. Turning it into the `huri-modules` repository, once
each package has its own repository:

1. For each `packages/huri-*`: `git subtree split --prefix packages/<name> -b split/<name>`,
   push that branch to its new repository and tag it (`v0.1.0`).
2. Create `huri-modules`: copy this directory's files, then
   `git submodule add <repo> modules/<name>` and `git -C modules/<name> checkout v0.1.0`
   for each, and change every `source` to
   `{ submodule = "modules/<name>", tag = "v0.1.0" }`.
3. Move `config/`, `deploy/`, `helm/` and `scripts/install_local.sh` (+
   `scripts/module_requirements.py`) here; `install.sh` becomes that script.
4. Its CI: `git clone --recursive`, install `huri-core` + every module
   (`pip install -e modules/*`), `check_registry.py --run-tests`, and the full
   pipeline test (`tests/integration/test_full_pipeline.py`).

Clone with `git clone --recursive` (or run `git submodule update --init`) — the
module folders are empty otherwise.
