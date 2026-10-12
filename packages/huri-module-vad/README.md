# huri-module-vad

Voice activity detection (WebRTC VAD): microphone frames in, speech segments out.

| Module | Topics | What it does |
|---|---|---|
| `vad` | `huri.audio.in` → `huri.voice` | Detects speech in microphone frames (WebRTC VAD) |

```sh
pip install -e packages/huri-module-vad     # from a HuRI checkout, next to huri-core and huri-events
HURI_MODULES=vad ...   # enable it on the server
```

This directory is a self-contained distribution, laid out to move as-is to its
own `huri-module-vad` repository.
