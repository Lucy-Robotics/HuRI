# HuRI architecture

How the pieces fit, as built. The design and its rationale are in the refactor
epic (Lucy-Robotics/HuRI#93).

## Layers

```
use cases       huri-cli, a website backend, a robot ...   ── client SDK ──┐
                                                                            │ websocket
huri-core       Module / EventData base classes, bus, server,             ◄┘
                plugin loader, compatibility checks, client SDK, testing
                  ▲ entry points ("huri.modules", "huri.events")
event packages  huri-events (huri.*), third-party (acme.*)
module packages huri-module-stt, huri-module-rag, ... , third-party modules
```

Dependencies point one way, with no cycle:

```
use case      ──► huri-core[client], event packages
module        ──► huri-core[server], event packages
event package ──► huri-core
```

`huri-core` knows no concrete module or event: it moves bytes and JSON and calls
`from_wire` / `to_wire`. Its extras keep clients light: `huri-core[client]` is
websockets + YAML loading, `huri-core[server]` is Ray Serve + FastAPI and the
pins that keep them working.

## Modules and events are plugins

A package registers what it provides in its `pyproject.toml`:

```toml
[project.entry-points."huri.modules"]
stt = "huri_stt.module:STT"

[project.entry-points."huri.events"]
"huri.transcript" = "huri_events.speech:Transcript"
```

Installing it is all it takes. The server loads modules
(`huri_core/core/plugins.py`) only if its operator enables them:
`HURI_MODULES` is a **default-deny** allow-list — unset enables nothing — and
it may also name an unpackaged module by import path
(`my_pkg.servo:Servo`, "tier 0"), which a client can then use with
`module: my_pkg.servo:Servo`. A client can never make the server import code
the server did not list.

**Topics are namespaced**, `<namespace>.<name>`. `huri.` belongs to
`huri-events`; every other package registers its topics under one namespace of
its own, which no other package may use. A replacement for a curated event
(`acme.motion`) lives next to it instead of overriding it, and a module's
topics are part of what it is: a module consuming `acme.motion` is a different
module, wired instead of the old one.

## Checks before anything runs

- **At startup** (`build_app`, before deploying): every enabled module's topics
  are registered by an installed event package, with the event classes the
  module declares (`input_event` / `output_event`); and, for a module shipping a
  `huri_module.toml` manifest, the manifest agrees with the class and the
  installed `huri-core` and event packages are within its declared ranges.
- **Per session**: every module input is produced by another module or by the
  client, and every topic the client subscribes to is emitted.

Failures say which module, which topic, and which package is probably missing.

**Actuation.** A module that can drive physical hardware declares it
(`requires.actuation` in its manifest, or `actuation = True` on its class); the
server will not start with it unless the operator also lists it in
`HURI_ACTUATION_MODULES`. Enabling a module is not consenting to it moving a
robot, and neither is a verified mark.

## A session

The client sends a handshake — `user_id`, `modules`, the topics it will `send`
and those it will `subscribe` to — and gets `session_init`, or `session_error`
with the reason. Events then travel as JSON frames or binary frames
`[u16 topic length][topic][payload]`. On the server, the event bus runs each
module on its input topic and publishes what it emits; a `Sender` per
subscribed topic forwards to the client. The SDK delivers each topic's events
to its handlers in order.

Heavy models run as Ray Serve deployments (`ModuleWithHandle`); module code
imports their libraries lazily, so the head node can import every module to
build the app whatever its hardware.

## Building and trusting modules

| Tier | What | Review |
|---|---|---|
| 0 | an import path listed in `HURI_MODULES` | none |
| 1 | a pip-installable package with entry points | none |
| 2 | discoverable (GitHub topic `huri-module`), found by `huri hub search` / `list` | none |
| 3 | listed in [huri-modules](../huri-modules) — "verified" | a pull request, CI-checked ([ACCEPTANCE.md](../huri-modules/ACCEPTANCE.md)) |

A module is tested alone with `huri_core.testing` (`ModuleHarness`, and
`LocalHandle` standing in for a deployment); [huri-modules](../huri-modules)
runs the whole curated pipeline together (`tests/integration`).

## Installing

Each module package declares its dependencies, and exact pins per hardware
variant in `requirements-{cpu,nvidia,amd}.txt`. The installer merges them for
the planned modules and stops on conflicting pins before installing anything
heavy; the Docker images install every curated package and fail to build if
modules and events do not fit together.
