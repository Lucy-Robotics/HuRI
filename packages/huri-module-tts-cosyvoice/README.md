# huri-module-tts-cosyvoice

HuRI module `tts_cosyvoice`: speaks the assistant's reply with CosyVoice3,
cloning the voice of a reference sample. Needs an NVIDIA GPU for real time
(it is ~7x slower than realtime on CPU, and its vocoder faults on consumer
RDNA3 GPUs).

| Module | Topics | Deployment |
|---|---|---|
| `tts_cosyvoice` | `huri.token` → `huri.audio.out` | `CosyVoiceTTSHandle` |

```sh
pip install -c constraints.txt --extra-index-url https://download.pytorch.org/whl/cu121 \
  -r packages/huri-module-tts-cosyvoice/requirements-nvidia.txt
pip install -e packages/huri-tts -e packages/huri-module-tts-cosyvoice
packages/huri-module-tts-cosyvoice/install_cosyvoice.sh /opt/cosyvoice   # not pip-installable
export PYTHONPATH=/opt/cosyvoice:/opt/cosyvoice/third_party/Matcha-TTS
HURI_MODULES=...,tts_cosyvoice ...   # instead of tts; see the env vars in handle.py
```

Use it in place of `tts` in the server's `HURI_MODULES` and the client's
`modules:`, and name its Serve deployment `CosyVoiceTTSHandle`.

Laid out to move as-is to its own `huri-module-tts-cosyvoice` repository.
