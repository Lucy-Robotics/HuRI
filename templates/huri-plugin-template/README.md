# huri-module-example

A starting point for a new HuRI module, built and tested on its own: you need
`huri-core` and this repository, not the rest of HuRI, a GPU or a running
pipeline. Copy it (GitHub: "Use this template"), then:

```sh
./rename.sh servo                       # your module's name, lowercase
pip install "huri-core[server] @ git+https://github.com/Lucy-Robotics/HuRI.git" \
            "huri-events @ git+https://github.com/Lucy-Robotics/HuRI.git#subdirectory=packages/huri-events"
./install.sh                            # picks requirements-{cpu,nvidia,amd}.txt for this machine
pytest                                  # tests/ runs the module with huri_core.testing
```

## What is in it

| Path | What it is |
|---|---|
| `huri_example/module.py` | the module: a `Module` subclass with its input/output topics and `process()` |
| `huri_example/huri_module.toml` | its manifest, read by HuRI's startup check and the hub |
| `pyproject.toml` | the `huri.modules` entry point (`shout = ...`) and the dependencies |
| `events/` | the events the module introduces, as their own light distribution in their own topic namespace (`example.*`) — so a client can decode them without installing the module |
| `tests/` | the module tested alone (`ModuleHarness`), and the checks the server runs at startup |
| `requirements-{cpu,nvidia,amd}.txt`, `install.sh` | hardware-specific pins, and the script choosing them |
| `.github/workflows/ci.yml` | the same, on every push |

## Using it in HuRI

Installed on the server, the module is known by its entry point name:

```sh
HURI_MODULES=stt,tag,shout ...          # the server's allow-list
```

```yaml
# client config
modules:
  shout: { name: shout, args: { exclamations: 2 } }
subscribe: [example.shout]
```

A client decoding `example.shout` installs only `events/`.

## When the module moves hardware

Set `requires = { actuation = true }` in the manifest (and `actuation = True` on
the class). The server then refuses to load it unless its operator lists it in
`HURI_ACTUATION_MODULES` as well as `HURI_MODULES`; document the module's
limits (ranges, rates) in this README.

## When the module needs a model

Run the model as a Ray Serve deployment and make the module a
`ModuleWithHandle` (`_handle_cls = YourDeployment`), calling
`await self._handle.method.remote(...)`; in tests, `LocalHandle(fake)` stands in
for the deployment. Import heavy libraries (torch, ...) inside the deployment,
not at module level: the server imports every enabled module to build the app.
`huri-module-stt` in the HuRI repository is a complete reference.

## Getting it listed

Tag the repository with the GitHub topic `huri-module` to be found by
`huri hub search`. To be listed as verified, open a pull request to
`huri-modules` (see its ACCEPTANCE.md).
