# huri-module-stt

Speech to text (faster-whisper) as a Ray Serve deployment.

| Module | Topics | What it does |
|---|---|---|
| `stt` | `huri.voice` → `huri.transcript` | Transcribes speech with faster-whisper (CPU or GPU) |

```sh
pip install -e packages/huri-module-stt     # from a HuRI checkout, next to huri-core and huri-events
HURI_MODULES=stt ...   # enable it on the server
```

Hardware-specific pins: `requirements-amd.txt`, `requirements-nvidia.txt` (install them before the package).

This directory is a self-contained distribution, laid out to move as-is to its
own `huri-module-stt` repository.
