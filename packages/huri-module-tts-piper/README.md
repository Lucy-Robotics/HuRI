# huri-module-tts-piper

HuRI module `tts`: speaks the assistant's reply with [Piper](https://github.com/OHF-Voice/piper1-gpl)
— ONNX only, no torch, no GPU, ~30x faster than realtime on a laptop CPU. The
default TTS: it runs on any machine.

| Module | Topics | Deployment |
|---|---|---|
| `tts` | `huri.token` → `huri.audio.out` | `TTSHandle` |

```sh
pip install -e packages/huri-tts -e packages/huri-module-tts-piper
python -m piper.download_voices --download-dir assets/models/piper en_US-lessac-medium
HURI_PIPER_VOICE=assets/models/piper/en_US-lessac-medium.onnx HURI_MODULES=...,tts ...
```

Laid out to move as-is to its own `huri-module-tts-piper` repository.
