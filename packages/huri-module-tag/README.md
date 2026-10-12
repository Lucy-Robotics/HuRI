# huri-module-tag

Text aggregator: stitches overlapping transcripts into one question.

| Module | Topics | What it does |
|---|---|---|
| `tag` | `huri.transcript` → `huri.partial_question` | Stitches overlapping transcript passes into one sentence |

```sh
pip install -e packages/huri-module-tag     # from a HuRI checkout, next to huri-core and huri-events
HURI_MODULES=tag ...   # enable it on the server
```

This directory is a self-contained distribution, laid out to move as-is to its
own `huri-module-tag` repository.
