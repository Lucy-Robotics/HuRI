# huri-module-rag

Retrieval-augmented answers with long-term memory (Qdrant + an OpenAI-compatible LLM), and the question aggregator.

| Module | Topics | What it does |
|---|---|---|
| `rag` | `huri.question` → `huri.token` | Answers questions from memory with an LLM, streaming tokens |
| `qag` | `huri.partial_question` → `huri.question` | Joins a transcript and its emotion into a question |

```sh
pip install -e packages/huri-module-rag     # from a HuRI checkout, next to huri-core and huri-events
HURI_MODULES=rag,qag ...   # enable it on the server
```

This directory is a self-contained distribution, laid out to move as-is to its
own `huri-module-rag` repository.
