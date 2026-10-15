# huri-core

The framework of [HuRI](https://github.com/Lucy-Robotics/HuRI-landing), a realtime,
modular conversational pipeline for robots and avatars: a multi-client server
that runs, per client session, a pipeline of modules exchanging typed events.

huri-core is **only the abstract framework**: the base classes modules and
events derive from, the event bus and the sessions, the Ray Serve server, the
plugin discovery and its safety checks, the client SDK, a module test harness
and the `huri hub` command. It ships no module and no concrete event, and it is
built and tested with none installed.

| You want to...                                         | Go to                                                                                         |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| understand HuRI as a whole                             | [Lucy-Robotics/HuRI-landing](https://github.com/Lucy-Robotics/HuRI-landing)                   |
| run a real pipeline (STT, RAG, TTS, gestures...)       | [Lucy-Robotics/huri-modules](https://github.com/Lucy-Robotics/huri-modules)                   |
| write a module                                         | [Lucy-Robotics/huri-plugin-template](https://github.com/Lucy-Robotics/huri-plugin-template)   |
| build on the framework itself, or change it            | this repository                                                                               |

## Install

huri-core is released as git tags, not on PyPI. Pick a ref: a release tag
(`0.4.0`, ...) for anything you depend on, `dev` for the development branch.
Python 3.10 to 3.12.

```sh
# A server: Ray Serve + FastAPI and the pins that keep them working.
pip install "huri-core[server] @ git+https://github.com/Lucy-Robotics/huri-core.git@<ref>"

# A client (robot, app): the client SDK, without Ray or FastAPI.
pip install "huri-core[client] @ git+https://github.com/Lucy-Robotics/huri-core.git@<ref>"

# Working on huri-core: every extra plus the lint/test toolchain.
git clone https://github.com/Lucy-Robotics/huri-core.git && cd huri-core
pip install -c constraints.txt -e ".[server,client,dev]"
```

The base install (no extra) has no dependency at all: the base classes and
`huri_core.testing` are enough to write and unit-test a module or an event
package.

A **module author** depends on a huri-core range in the package's
`pyproject.toml`, e.g. `"huri-core[server]>=0.4.0.dev0,<0.5"` (`>=0.4,<0.5`
once `0.4.0` is tagged, see [Versioning](#versioning-and-compatibility)), and
installs huri-core from git first since pip cannot find it on an index. Modules that use the
curated `huri.*` topics also depend on `huri-events`, which lives in
huri-modules:

```sh
pip install "huri-events @ git+https://github.com/Lucy-Robotics/huri-modules.git@<ref>#subdirectory=packages/huri-events"
```

[huri-plugin-template](https://github.com/Lucy-Robotics/huri-plugin-template)
has all of this set up.

`constraints.txt` repeats the server's pins (protobuf, FastAPI, setuptools; its
header says what breaks without each). Pass it with `-c` to whatever you
install into a server environment after huri-core, so a module's dependencies
cannot move them.

## Concepts

### Modules

A module is a processing node: it consumes the events of one topic and emits
events on (at most) one other.

```python
from huri_core.core.module import Module


class Shout(Module):
    input_type = "echo.text"     # the topic it consumes
    output_type = "echo.loud"    # the topic it produces; None for a terminal module
    input_event = Text           # optional: the event classes it expects, checked
    output_event = Text          # at startup against what is installed

    async def process(self, data: Text):
        return Text(data.text.upper())   # or None to emit nothing
```

`process()` is either a coroutine returning one event (or `None`), or an async
generator yielding any number of them (streaming: tokens, audio chunks...).
A module is built once per session, with the `args:` of the client's config as
keyword arguments; an optional `async def finalize(self)` runs when the
session ends (to persist per-user state, for instance).

Two mixins cover what the server injects:

- `ModuleWithHandle`: the module calls a model served as its own Ray Serve
  deployment. It sets `_handle_cls` to that deployment; the server binds it
  once (only for enabled modules) and passes the handle as `self._handle`,
  shared by every session.
- `ModuleWithId`: the module receives the client's identity as
  `self._user_id`, to keep per-user state (memory, preferences).

`actuation = True` marks a module that can drive physical hardware (see
[Safety](#safety-default-deny-and-actuation)).

### Events and the wire

An event is a dataclass deriving from `EventData`, which owns its wire format:
`to_wire()` and the `from_wire()` classmethod. Two bases cover nearly
everything:

- `JsonEvent`: the dataclass fields as a JSON object (`asdict` /
  `cls(**data)`); override both methods for nested or typed fields;
- `BytesEvent`: a `bytes` payload (`.data`), for audio and other binary
  streams; subclass it to give the bytes a layout.

On the websocket, an event whose `to_wire()` gives a mapping travels as a JSON
text frame, anything else as a binary frame:

```text
JSON:    {"topic": "echo.loud", "data": {...}}
binary:  [u16 big-endian topic length][topic, UTF-8][payload]
```

`summarize()` is what the server logs for an event; override it for large
payloads.

### Topics and namespaces

Topics are namespaced, `<namespace>.<name>` in lowercase. `huri.` is reserved
for the curated events of `huri-events`; every other distribution registers its
topics under one namespace of its own (`acme.servo_angles`), which no other
distribution may use. So a third-party replacement for a curated event
(`acme.motion`) lives next to it instead of overriding it, and two packages
cannot silently fight over a topic: the server refuses to start instead.

### Sessions, the bus and the Sender

A client opens a websocket on `/session` and sends a handshake: the modules of
its pipeline, the topics it will `send`, the topics it `subscribe`s to.

```json
{
  "user_id": "...",
  "modules": {"shout": {"name": "shout", "args": {}}},
  "send": ["echo.text"],
  "subscribe": ["echo.loud"]
}
```

The server builds the pipeline (one instance of each module for this session),
checks its wiring, and answers `{"type": "session_init", "user_id": ...}`, or
`{"type": "session_error", "error": ..., "server_modules": [...]}` and closes
the socket (code 1008). A session has its own event bus (`EventGraph`): each
event published on a topic runs every module subscribed to it, concurrently,
and what they emit is published in turn. For each subscribed topic a `Sender`
forwards the events to the client, framed as above. An incoming frame on a
topic none of the server's enabled modules uses is dropped and logged; the
session goes on.

### Plugins: entry points

The server finds modules and events through Python entry points, so any
installed distribution can provide them:

```toml
[project.entry-points."huri.modules"]
servo = "huri_servo.module:Servo"          # a client asks for it as `name: servo`

[project.entry-points."huri.events"]
"acme.servo_angles" = "huri_servo.events:ServoAngles"
```

Installing the package is all it takes for the server to *know* it; enabling
it is a separate, explicit decision (below). Only enabled modules are
imported, so a host never imports a module it does not run, nor its heavy
dependencies. Two distributions registering the same name for different
targets is an error naming both.

### Safety: default-deny and actuation

`HURI_MODULES` is the server's allow-list and it is **default-deny**: unset or
empty, the server enables no module and rejects every session, because any
installed package can register one. List each module by name, or by import
path for a module that is not packaged at all:

```sh
HURI_MODULES=shout,my_pkg.servo:Servo
```

A client refers to the latter with `module: my_pkg.servo:Servo` instead of
`name:`; it can never make the server import a path the server did not list.
A client asking for an installed but not enabled module is told so.

A module that can **move hardware** declares it (`actuation = True` on the
class, or `requires.actuation = true` in its manifest), and the server refuses
to start with it enabled unless its operator also lists it in
`HURI_ACTUATION_MODULES`: a second, explicit consent, independent of
`HURI_MODULES` and of any verified mark. Active actuation modules are logged as
a warning at startup.

### Manifests and the startup and session checks

A module package ships a `huri_module.toml` per module, next to its code, so
tooling can reason about the module without importing it:

```toml
name = "servo"                     # its huri.modules entry point name
summary = "Gesture to servo angle arrays"
input = { topic = "huri.motion", event = "huri_events.motion:Motion" }
output = { topic = "acme.servo_angles", event = "huri_servo.events:ServoAngles" }  # omit for a terminal module
events = ["huri-events>=0.1,<0.2"]               # optional
requires = { gpu = false, actuation = false }    # optional
huri_core = ">=0.4.0.dev0,<0.5"
```

(An unpackaged module's manifest sits next to its file, named after its class
lowercased.) The server checks, and reports every problem at once instead of
starting a pipeline that silently drops events:

- **at startup** (`check_modules`, `check_manifests`, `check_actuation`):
  every topic an enabled module consumes or produces is registered by an
  installed event package, with the class the module expects; a manifest
  agrees with its class; the installed huri-core and event packages are in
  the manifest's ranges; actuation is opted in;
- **per session** (`check_session`): every module input is produced by another
  module or sent by the client, and every subscribed topic is emitted by the
  pipeline.

### Running a server

```sh
HURI_MODULES=shout python -m huri_core.launch_huri   # starts Ray and serves the app
```

`huri_core.app:app` is the Serve application (`build_app()`, which runs the
startup checks): a [Serve config file](https://docs.ray.io/en/latest/serve/production-guide/config.html)
uses it as its `import_path` and sets `HURI_MODULES` (and
`HURI_ACTUATION_MODULES`) in its `runtime_env.env_vars`, for `serve deploy`.
The session endpoint is `ws://<host>:8000/session`.

### Client SDK

`huri_core.core.client.HuRIClient` (`huri-core[client]`) owns the websocket,
the handshake and the framing, and calls your handlers:

```python
client = HuRIClient(url, modules={"shout": {"name": "shout"}}, sends=["echo.text"])
client.on("echo.loud", Text, handler)   # before connecting
async with client:
    await client.send("echo.text", Text("hello"))
    await client.wait_closed()
```

Handlers of one topic run one at a time, in arrival order (audio playback
needs that); different topics run concurrently; a failing handler is logged
and does not stop the others. A refused session raises `HuRISessionError`
(with `server_modules`). The same session can be written as a YAML file
(`huri_url`, `modules`, `send`, `subscribe`), loaded with
`load_client_config(path)` and turned into a client with
`HuRIClient.from_config(...)`; `load_client_config` also gives the session
this machine's `user_id`, created once and kept in the user config directory.
huri-modules ships a commented
[client_template.yaml](https://github.com/Lucy-Robotics/huri-modules/blob/main/config/client_template.yaml).

### Testing a module: `huri_core.testing`

`ModuleHarness` runs one module in-process, with no Ray and no server (only
the base install): it builds the module like the server does, checks the
events in and out against `input_event` / `output_event`, and returns what it
emitted. `LocalHandle` stands in for the Serve deployment of a
`ModuleWithHandle`, on top of a plain fake object, and records its calls.

```python
from huri_core.testing import LocalHandle, ModuleHarness


class FakeModel:  # the deployment methods the module calls
    async def generate(self, prompt):
        return "hi"


harness = ModuleHarness(Chat, handle=LocalHandle(FakeModel()), user_id="u1")
assert harness.run(Text("hello")) == [Text("hi")]
```

### Finding modules: `huri hub`

```sh
huri hub search gesture             # GitHub repositories tagged `huri-module`
huri hub list                       # modules supporting the installed huri-core
huri hub list --verified            # only the curated registry of huri-modules
huri hub list --core 0.5.0 --all    # another version; --all keeps unsupported ones
huri hub install stt                # pip-installs a verified module (and huri-events); never enables it
huri hub install servo --community  # an unreviewed one: only when asked explicitly
```

`list` merges the verified registry with community modules, read from their
`pyproject.toml` and `huri_module.toml` on GitHub without installing anything,
and keeps those whose manifest supports the huri-core version. `install`
refuses a module whose range excludes the installed huri-core, rather than let
pip replace it. The curated packages modules depend on by name (`huri-events`,
and for a curated module the curated libraries it needs, such as `huri-tts`)
are on no package index: `install` passes them to pip in the same command,
from huri-modules at the module's ref, so pip never looks them up elsewhere.
Nothing found is ever loaded: enabling stays the operator's `HURI_MODULES`
decision.

The registry is `registry.toml` of
[huri-modules](https://github.com/Lucy-Robotics/huri-modules), read at the ref
`HURI_MODULES_REF` (a huri-modules release tag such as `0.1.0`, or a branch;
`main` by default), and its curated modules are installed from that same ref.
Inside a huri-modules checkout (a directory with `registry.toml` and
`check_registry.py`), that checkout's registry and packages are used instead.
`--registry PATH|URL` or `HURI_REGISTRY` points at any other registry. For
huri-modules' own registry on GitHub
(`https://raw.githubusercontent.com/Lucy-Robotics/huri-modules/<ref>/registry.toml`)
the curated packages come from that `<ref>`; for any other location, those
missing from it come from huri-modules at `HURI_MODULES_REF` (the hub says
so). `list` and `install` print the registry they read on stderr, and warn
when a setting is ignored (`HURI_MODULES_REF` inside a checkout, the old
`HURI_REF`). `GITHUB_TOKEN` raises GitHub's rate limit. `huri hub` needs
`huri-core[client]`.

## A minimal end to end example

A package providing one module and its events, a server running it, and a
client talking to it. Nothing else is installed.

```text
my-echo/
├── pyproject.toml
└── my_echo/__init__.py
```

```toml
# my-echo/pyproject.toml
[project]
name = "my-echo"
version = "0.1.0"
dependencies = ["huri-core>=0.4.0.dev0,<0.5"]

[project.entry-points."huri.modules"]
shout = "my_echo:Shout"

[project.entry-points."huri.events"]   # the `echo` namespace is this package's
"echo.text" = "my_echo:Text"
"echo.loud" = "my_echo:Text"
```

```python
# my-echo/my_echo/__init__.py
from dataclasses import dataclass

from huri_core.core.events import JsonEvent
from huri_core.core.module import Module


@dataclass
class Text(JsonEvent):
    text: str


class Shout(Module):
    input_type, output_type = "echo.text", "echo.loud"
    input_event, output_event = Text, Text

    async def process(self, data: Text):
        return Text(data.text.upper() + "!")
```

```sh
pip install "huri-core[server,client] @ git+https://github.com/Lucy-Robotics/huri-core.git@<ref>"
pip install -e ./my-echo
HURI_MODULES=shout python -m huri_core.launch_huri
```

```python
# client.py, in another terminal
import asyncio

from huri_core.core.client import HuRIClient
from my_echo import Text


async def main() -> None:
    client = HuRIClient(
        "ws://localhost:8000/session",
        modules={"shout": {"name": "shout"}},
        sends=["echo.text"],
    )
    done = asyncio.Event()

    async def show(event: Text) -> None:
        print(event.text)  # HELLO!
        done.set()

    client.on("echo.loud", Text, show)
    async with client:
        await client.send("echo.text", Text("hello"))
        await asyncio.wait_for(done.wait(), 10)


asyncio.run(main())
```

From here:

- **a real pipeline** (voice in, speech and gestures out; installer, Docker
  images, Helm chart, reference CLI):
  [huri-modules](https://github.com/Lucy-Robotics/huri-modules);
- **your own module**, with its manifest, events package, tests and CI:
  [huri-plugin-template](https://github.com/Lucy-Robotics/huri-plugin-template);
- **the big picture** (architecture, contributing):
  [HuRI-landing](https://github.com/Lucy-Robotics/HuRI-landing).

## Versioning and compatibility

huri-core follows semver with tags `X.Y.Z` (no `v` prefix); `dev` is the
development branch, `main` the latest release. While huri-core is `0.x`, a
**minor** release (`0.4` → `0.5`) may break the public surface and a patch
release never does, so modules and event packages pin a minor range:
`huri-core>=0.4,<0.5`. Until `0.4.0` is tagged, use `>=0.4.0.dev0,<0.5`: the
only installable huri-core is `dev` (`0.4.0.dev0`), a pre-release below `0.4`
that `>=0.4` excludes. The public surface (base classes, client SDK and
session schema, wire protocol, entry point groups, manifest) is listed in
[RELEASING.md](RELEASING.md).

[huri-modules](https://github.com/Lucy-Robotics/huri-modules) has its own
versions; each of its releases is a tested set built against one huri-core
release:

| huri-modules | huri-core |
| ------------ | --------- |
| 0.1.x        | 0.4.x     |

huri-modules' CI also runs against huri-core's `dev`, so a breaking change here
is seen there before it is released.

## Developing

```sh
make lint     # black, isort, flake8 (rewrite in place) + mypy
make test     # pytest
make check    # both
```

CI (`.github/workflows/ci.yml`) runs the lint, the tests on Python 3.10 to
3.12, the tests with only `huri-core[client]` installed (no Ray, no FastAPI),
and a wheel build.

Layout:

- `huri_core/core/` — `module` and `events` (the base classes), `bus` (the
  event graph), `session`, `sender`, `huri` (the Serve ingress and the
  per-session router), `factory` (builds a session's modules), `plugins`
  (entry point discovery, namespaces), `manifest`, `compat` (startup and
  session checks), `client` (the client SDK), `dataclasses/config` (the
  session schema), `user_config`
- `huri_core/app.py`, `huri_core/launch_huri.py` — the server entry points
- `huri_core/testing.py` — `ModuleHarness`, `LocalHandle`
- `huri_core/hub.py`, `huri_core/cli.py` — the `huri hub` command
- `tests/core/` — everything above, with toy modules and events only
