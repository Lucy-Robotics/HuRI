# HuRI

**HuRI** is a realtime, modular conversational pipeline for robots: speech in,
an answer out as text, a voice and gestures. A server (Ray Serve) runs a
pipeline of **modules** — voice activity detection, speech-to-text, emotion,
retrieval-augmented answers, text-to-speech, gesture generation — wired by
**topics**; clients connect over a websocket and pick the modules of their
session.

Everything is a plugin: any pip-installable package can add a module or an
event type, without touching HuRI itself.

## Start here

| You want to… | Go to | You need |
|---|---|---|
| **Try it** — a working pipeline, fast | [huri-modules](../huri-modules) — `install.sh` on this machine, or the Helm chart on a cluster | that repository only |
| **Build a module** — write or change one module, alone | [huri-plugin-template](../templates/huri-plugin-template) | `huri-core` + the template; no GPU, no running pipeline |
| **Deploy** — run a chosen set of modules somewhere | [huri-modules](../huri-modules) — `install.sh` (bare metal) or [deploy/](../deploy) and [helm/](../helm) | that repository only |
| **Write a client** — a website, a robot, a CLI | [the client SDK](../README.md#client-sdk) and [huri-cli](../examples/cli), the reference client | `huri-core[client]` + [huri-events](../packages/huri-events) |

## The pieces

| Repository | What it is |
|---|---|
| [huri-core](../README.md) | the framework: module and event base classes, bus, server, plugin loader, client SDK, `huri_core.testing`; no concrete module or event |
| [huri-events](../packages/huri-events) | the curated event vocabulary, one class per `huri.*` topic |
| [huri-module-vad](../packages/huri-module-vad) | voice activity detection (WebRTC) |
| [huri-module-stt](../packages/huri-module-stt) | speech to text (faster-whisper) |
| [huri-module-tag](../packages/huri-module-tag) | transcript stitching |
| [huri-module-emo](../packages/huri-module-emo) | speech emotion, and its aggregator |
| [huri-module-rag](../packages/huri-module-rag) | answers from long-term memory (Qdrant + an LLM), and the question aggregator |
| [huri-tts](../packages/huri-tts), [huri-module-tts-piper](../packages/huri-module-tts-piper), [huri-module-tts-cosyvoice](../packages/huri-module-tts-cosyvoice) | text to speech: shared streaming logic, the default Piper engine, CosyVoice3 voice cloning |
| [huri-module-mov](../packages/huri-module-mov) | co-speech gestures (EMAGE) |
| [huri-modules](../huri-modules) | the curated, verified module set: registry, install and deploy |
| [huri-plugin-template](../templates/huri-plugin-template) | starter for a new module |
| [huri-cli](../examples/cli) | the reference command-line client |

Today these all live in the [HuRI repository](https://github.com/Lucy-Robotics/HuRI),
each in a directory laid out to become its own repository; the links follow
them when they move.

Read next: [ARCHITECTURE.md](ARCHITECTURE.md) — how the pieces fit — and
[CONTRIBUTING.md](CONTRIBUTING.md).
