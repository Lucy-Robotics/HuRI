# Contributing to HuRI

Pick the line that matches what you want to change.

| You want to… | Where | How |
|---|---|---|
| Add a module (a new capability) | your own repository, from [huri-plugin-template](../templates/huri-plugin-template) | nobody's permission needed: publish it, tag it `huri-module` to be found; to get the verified mark, open a pull request to [huri-modules](../huri-modules) ([ACCEPTANCE.md](../huri-modules/ACCEPTANCE.md)) |
| Add an event type | an events package of your own, in your own topic namespace (`acme.*`) | see the template's `events/`; `huri.*` is reserved for [huri-events](../packages/huri-events) |
| Fix or improve a curated module | that module's package ([packages/](../packages)) | pull request with tests (`huri_core.testing`) |
| Change the framework (bus, server, SDK, loader) | [huri-core](../huri_core) | pull request; mind the public surface and semver ([RELEASING.md](../RELEASING.md)) |
| Change install or deployment | [huri-modules](../huri-modules), [deploy/](../deploy), [helm/](../helm), [scripts/](../scripts) | pull request; CI builds the base image and runs a session against it |
| Improve these docs | this directory | pull request |

Every change to HuRI itself runs `make check` (black, isort, flake8, mypy,
pytest) in CI; run it locally first.
