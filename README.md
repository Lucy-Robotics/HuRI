# HuRI

## Presentation

HuRI is an open-source research project focused on conversational AI for humanoid robots and virtual avatars.
HuRI provides a modular architecture that allows developers to design, implement, and run AI modules within customizable conversational pipelines defined by the user.
HuRI is launched as a multi-client server, to handle multiple client (robots) conversational requests.
The framework supports the implementation and integration of multiple AI modules, including:
Speech-to-Text (STT) and Text-to-Speech (TTS), Retrieval-Augmented Generation (RAG), Emotional analysis (EMO), Motion and gesture generation (MOV)

## Quick start

```sh
git clone https://github.com/Sentience-Robotics/HuRI.git && cd HuRI
scripts/install_local.sh --plan-only      # what would run on this machine?
scripts/install_local.sh                  # show the plan, then install it
.huri-local/start.sh                      # start everything
```

Then, in a second terminal:

```sh
source .huri-local/env.sh
python -m src.client --config config/client_local.generated.yaml
```

You get a `>>` prompt; typing or speaking both work. `\exit` quits the client
and leaves the server running.

**Use the installer, not a bare `pip install`.** It picks the right torch build
for your GPU, applies `constraints.txt` (without which every Ray Serve replica
crashes — see the comments in that file), creates the virtualenv, downloads the
models and writes a Serve config that matches your hardware. The manual path is
documented at the bottom for people who need it.

## Prerequisites

The installer installs the system packages itself; you only need Python, a
package manager it recognises (`apt`, `pacman`, `dnf`, `zypper`) and `sudo`.

|          | Requirement                                                                                        |
| -------- | -------------------------------------------------------------------------------------------------- |
| Python   | **3.10 – 3.12** (3.12 recommended; 3.13+ is not supported yet — `numpy 1.26` has no wheels for it) |
| Disk     | ~8 GiB for the default voice pipeline, ~20 GiB with CosyVoice TTS                                  |
| RAM      | ~16 GiB for the default plan; the installer refuses a plan that will not fit                       |
| Network  | huggingface.co (models) and the Ollama/PyPI mirrors                                                |
| Optional | An input device if you want speech in, a GPU for faster STT                                        |

Ubuntu 24.04:

```sh
sudo apt install python3.12 python3.12-venv python3.12-dev python3-pip git
```

Ubuntu 22.04 — its Python is 3.10, which **is supported**, so nothing extra is
needed:

```sh
sudo apt install python3 python3-venv python3-dev python3-pip git
```

Arch / EndeavourOS — `pacman`'s `python` is newer than 3.12, so install a
supported interpreter separately and point the installer at it:

```sh
uv python install 3.12        # or: yay -S python312, or pyenv
scripts/install_local.sh --python "$(uv python find 3.12)"
```

Everything else (a C toolchain, `ffmpeg`, `libsndfile`, `portaudio`, and the
ROCm runtime on AMD) is installed for you by the `system` stage. Ollama and
Qdrant are installed and started too unless you point at remote ones.

## What runs where

The installer probes the machine and runs the largest pipeline that actually
works on it. Nothing here is aspirational — these are measured outcomes:

| Module                                                     | CPU only                         | NVIDIA       | AMD / ROCm                                |
| ---------------------------------------------------------- | -------------------------------- | ------------ | ----------------------------------------- |
| **STT** (faster-whisper)                                   | ✅ `int8`, realtime for `base`   | ✅ `float16` | ✅ `float16`                              |
| **TTS — piper** (default without an NVIDIA GPU)            | ✅ **~30x faster than realtime** | ✅           | ✅ (runs on CPU; it does not need a GPU)  |
| **TTS — cosyvoice** (default on NVIDIA with ≥4.4 GiB free) | ⚠️ ~7x _slower_ than realtime    | ✅ `fp16`    | ❌ vocoder faults in a MIOpen convolution |
| **RAG / LLM** (Ollama)                                     | ✅ tier picked from free RAM     | ✅           | ✅                                        |
| **EMO** (prosody)                                          | ✅ always CPU                    | ✅           | ✅                                        |
| **MOV / gesture** (EMAGE)                                  | ❌ slower than realtime          | ✅           | ❌ not in the ROCm build                  |

So **a machine with no GPU at all still gets voice in and voice out** — that is
the point of Piper being the default. A GPU buys faster STT, and on NVIDIA it
additionally unlocks CosyVoice and gesture generation.

## Choosing what to run

The plan is a starting point, not a cage.

```sh
# Only the modules you name (subset of vad,stt,tag,emo,eag,qag,rag,tts,mov)
scripts/install_local.sh --modules mic,stt,tag,qag,rag    # voice in, text out
scripts/install_local.sh --modules rag                    # text in, text out

# Where things run
scripts/install_local.sh --stt-device cpu                 # keep the GPU free
scripts/install_local.sh --tts-engine cosyvoice --voice-sample me.wav \
                         --voice-transcript "exactly what is said in it"

# Keep a module the plan dropped
scripts/install_local.sh --force-tts --force-gesture --force-emo
```

`--modules` sets `HURI_MODULES`, which is what decides the module registry
(`src/modules/modules.py`) and therefore which Serve deployments exist. The
**client** config must list the same module names — a client asking for a module
the server did not register is rejected with a `session_error` naming both sets.

`scripts/install_local.sh --help` lists every flag.

## Local install (single machine, no Kubernetes)

`scripts/install_local.sh` is the bare-metal counterpart of the Docker images in
`deploy/` and the Helm chart in `helm/`. It probes the machine (GPU vendor, VRAM,
RAM, disk, Python), works out which parts of the pipeline actually fit, and then
installs only those: system packages, a virtualenv with the right torch build,
the model weights, Qdrant and Ollama, plus a Ray Serve config matching the plan.

It generates:

| Path                                             | What                                              |
| ------------------------------------------------ | ------------------------------------------------- |
| `config/huri_local.generated.yaml`               | Ray Serve config for this machine                 |
| `config/client_local.generated.yaml`             | matching client config                            |
| `.huri-local/huri.env`                           | the same runtime env vars as a plain `.env`       |
| `.huri-local/secrets.env`                        | LLM API key, `0600`, never in the configs         |
| `.huri-local/env.sh`                             | venv + `huri.env` + `secrets.env` in one `source` |
| `.huri-local/start.sh` / `stop.sh` / `status.sh` | run the stack                                     |
| `.huri-local/plan.env`                           | the hardware plan it acted on                     |

```sh
.huri-local/start.sh                 # services + ray head + serve deploy
source .huri-local/env.sh
python -m src.client --config config/client_local.generated.yaml
.huri-local/status.sh                # what is up, locally and remotely
.huri-local/stop.sh --all
```

`start.sh` waits for the application to report `RUNNING` and exits non-zero with
the replica errors if it does not, so a failed deploy is never reported as
success. `status.sh` reads the _application_ status, not the HTTP proxy's health
route (which is green even when every deployment is dead).

`source .huri-local/env.sh` gives a shell configured exactly like a Serve
replica (model paths, `HURI_MODULES`, STT device, API key), so
`python -m src.launch_huri` and `python -m src.modules.rag.ingestion` work
without `serve deploy`.

Re-run a single stage with `--only`:

```sh
scripts/install_local.sh --only config          # regenerate the configs
scripts/install_local.sh --only verify          # re-check this machine
scripts/install_local.sh --only python,models   # reinstall deps and weights
```

### Feeding the RAG memory

Conversation memory works out of the box (`RAGHandle` creates its collection on
demand). Document retrieval needs an explicit ingest, and **the embedding model
must match the one the server queries with**, or the vectors will not line up:

```sh
source .huri-local/env.sh
python -m src.modules.rag.ingestion --help
python -m src.modules.rag.ingestion \
  --embedding-model "$HURI_EMBED_MODEL" --embedding-url "$HURI_EMBED_URL" ...
```

Until you do, `--only verify` and the `RAGHandle` startup log both warn that the
`documents` collection is missing and that retrieval will return nothing.

### Using a remote LLM instead of a local one

Any endpoint that is not on localhost is treated as already running: it is not
installed, not started, and costs no local VRAM — which is what frees a small
GPU to run TTS and gesture generation.

```sh
# self-hosted vLLM over HTTPS with a private CA / self-signed cert
scripts/install_local.sh \
  --llm-url https://llm.example.lan --llm-provider vllm \
  --llm-model Qwen3.5-4B-GGUF --no-verify-ssl \
  --embed-url https://embedding.example.lan --embed-model bge-large-en-v1.5-gguf-Q4_K_M

# hosted OpenAI-compatible API (key goes to .huri-local/secrets.env, 0600)
scripts/install_local.sh \
  --llm-url https://api.example.com --llm-provider api \
  --llm-model some-model --llm-api-key "$MY_KEY"

# remote Qdrant too — then nothing but HuRI itself runs locally
scripts/install_local.sh --qdrant-url https://qdrant.example.lan
```

The provider selects the wire protocol (`ollama` → `/api/chat`, `vllm`/`api` →
`/v1/chat/completions`, only `api` sends the bearer token) and is inferred from
the URL and whether a key was given. Re-point an existing install without
reinstalling anything: `scripts/install_local.sh --only config --llm-url ... --llm-model ...`
then restart (`.huri-local/stop.sh && .huri-local/start.sh`).

## Troubleshooting

| Symptom                                                        | Cause / fix                                                                                                                                                          |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Task was killed due to the node running low on memory`        | The plan did fit but Ollama's KV cache grew. Use a smaller tier: `--llm-model mistral:7b`, and `ollama stop <big-model>`.                                            |
| `torch sees the GPU but cannot launch a kernel on it`          | The torch build has no code objects for your GPU. `--only config` sets `HSA_OVERRIDE_GFX_VERSION` when a compatible same-family arch exists; otherwise run CPU-only. |
| Client exits with `session_error: Unknown module 'x'`          | The client config lists a module the server did not register. Match it to `HURI_MODULES`, or regenerate both with `--only config`.                                   |
| `qdrant is not answering … HuRI does not manage this instance` | Something else already owned the port when you installed, so HuRI never created its own. Start it yourself, or free the port and re-run `--only services`.           |
| No audio from the client                                       | Check TTS is actually enabled (`grep HURI_MODULES config/huri_local.generated.yaml`) — with `--modules` or a dropped plan there may be no TTS at all.                |
| Install fails with no explanation                              | Every command's output is in `.huri-local/install.log`, and the error line names the real command.                                                                   |

## Usage without the installer

Both entrypoints need the repo root on `PYTHONPATH` and the venv active; the
generated `config/huri_local.generated.yaml` is the reference for what the
env vars must contain.

```sh
ray start --head --num-cpus=8 --num-gpus=1     # --num-gpus >= the config's total
serve deploy config/huri_local.generated.yaml  # or your own Serve config
```

We use a Ray Serve config file, doc [here](https://docs.ray.io/en/latest/serve/configure-serve-deployment.html).
`serve run <config>` works too but blocks; `serve deploy` returns immediately.

Without Serve at all (single process, no `deployments:` overrides — note that
the RAG settings then come from `RAGDeploymentConfig`'s env fallbacks, not from
a Serve `user_config`):

```sh
python -m src.launch_huri
```

Client:

```sh
python -m src.client --config config/client_local.generated.yaml
```

Client config template: [config/client_template.yaml](config/client_template.yaml).
Server-side examples: [config/huri.yaml](config/huri.yaml) (GPU) and
[config/huri_cpu.yaml](config/huri_cpu.yaml).

### Manual dependency install

Only if you are not using `scripts/install_local.sh`. `-c constraints.txt` is
not optional — see that file's header for what breaks without it.

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -c constraints.txt -r serve_requirements.txt -r requirements.txt
# NVIDIA, for CosyVoice TTS + EMAGE gesture:
pip install -c constraints.txt -r requirements-nvidia.txt
# AMD: install the ROCm runtime from your distro, then the official ROCm wheels
# (repo.radeon.com's ".lw." wheels omit consumer RDNA3 — see install_local.sh)
pip install -c constraints.txt --index-url https://download.pytorch.org/whl/rocm6.4 \
  torch==2.8.0+rocm6.4 torchaudio==2.8.0+rocm6.4
pip install -c constraints.txt -r requirements-amd.txt
```

Note that `requirements.txt` also contains the lint/test toolchain.

## Testing a change

```sh
make lint        # black, isort, flake8, mypy
make test        # pytest
make check       # both
scripts/test_install.sh            # run the installer in clean Ubuntu containers
```

## Folder/Module structure

- **root** — entrypoints: `src/app.py`, `src/launch_huri.py` (server),
  `src/client.py` (client)
- **src/core** — `huri` (ingress + per-session router), `bus` (event graph),
  `client` / `interface`, `module`, `session`, `events`, `user_config`,
  `dataclasses/config`
- **src/interfaces** — `cli_interface`: the reference client senders (audio,
  text) and hooks (token, audio, motion)
- **src/modules**
  - `speech_to_text` — `microphone_vad` (VAD), `speech_to_text` (STT),
    `text_aggregator` (TAG)
  - `text_to_speech` — `piper_tts` (default engine), `text_to_speech`
    (CosyVoice3 + the `TTS` module)
  - `rag` — `rag`, `question_aggregator` (QAG), `ingestion`, `semantic_chunker`,
    `qdrant_utils`, memory maintenance/inspection
  - `emotion` — `prosody_analysis` (EMO), `emotion_aggregator` (EAG)
  - `gesture` — `gesture` + the vendored `emage` model code
  - `utils` — `sender` (forwards events to clients)
  - `factory`, `modules` — the module registry and `HURI_MODULES` filtering

## Developper Documentation

HuRI's complete documentation is available [here](https://docs.sentience-robotics.fr/share/p1x9ikjkhf/p/hu-ri-documentation-f4amcndYQg).
