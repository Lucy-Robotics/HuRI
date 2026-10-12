# huri-module-mov

Co-speech gestures (EMAGE): speech audio in, SMPL-X motion out.

| Module | Topics | What it does |
|---|---|---|
| `mov` | `huri.audio.out` → `huri.motion` | Generates SMPL-X gestures matching the speech (EMAGE) |

```sh
pip install -e packages/huri-module-mov     # from a HuRI checkout, next to huri-core and huri-events
HURI_MODULES=mov ...   # enable it on the server
```

Hardware-specific pins: `requirements-nvidia.txt`, `requirements-amd.txt` (install them before the package).

This directory is a self-contained distribution, laid out to move as-is to its
own `huri-module-mov` repository.
