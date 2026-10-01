#!/usr/bin/env bash
# =============================================================================
# HuRI — local (bare-metal) installer
# =============================================================================
#
# Installs and configures the whole HuRI stack on a single machine, without
# Kubernetes, Helm or Docker images. It is the local counterpart of:
#
#   deploy/Dockerfile.{base,nvidia,amd}   → the Python environment
#   helm/templates/*-model-init-job.yaml  → the model weight downloads
#   deploy/examples/*/values.yaml         → the Ray Serve config
#
# Before installing anything it *probes the machine* (GPU vendor, VRAM, RAM,
# disk, CPU, Python) and computes which parts of the pipeline can actually run
# here. Components that do not fit are moved to CPU or dropped from the module
# set, and the generated Ray Serve config reflects that decision.
#
# Usage:
#   scripts/install_local.sh --plan-only      # just tell me what would run here
#   scripts/install_local.sh                  # probe, show the plan, install
#   scripts/install_local.sh --yes            # no prompts
#   scripts/install_local.sh --help
#
# Everything it generates lives in .huri-local/ plus config/*.generated.yaml,
# so the install is inspectable and removable (`rm -rf .huri-local`).
# =============================================================================

set -Eeuo pipefail

# --- Paths -------------------------------------------------------------------

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_PATH")")"
STATE_DIR="$REPO_ROOT/.huri-local"
ASSETS_DIR="$REPO_ROOT/assets"
MODELS_DIR="$ASSETS_DIR/models"
VENV_DIR="$REPO_ROOT/.venv"
LOG_FILE="$STATE_DIR/install.log"

# --- Pinned versions (kept in sync with deploy/) -----------------------------

# deploy/Dockerfile.nvidia
COSYVOICE_REPO="https://github.com/FunAudioLLM/CosyVoice.git"
COSYVOICE_COMMIT="074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
TORCH_CUDA_VERSION="2.3.1"          # requirements-nvidia.txt
TORCH_CUDA_INDEX="https://download.pytorch.org/whl/cu121"
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"
# deploy/Dockerfile.amd
ROCM_VERSION="7.2"                  # system ROCm runtime we ask the distro for
# Official PyTorch ROCm wheels — chosen over repo.radeon.com because those omit
# consumer RDNA3 (gfx1102). See the comment at the amd) branch of
# install_python_deps for the measured arch lists.
TORCH_ROCM_VERSION="2.8.0"
TORCH_ROCM_FLAVOUR="rocm6.4"        # wheels are self-contained; need not match ROCM_VERSION
TORCH_ROCM_INDEX="https://download.pytorch.org/whl/rocm6.4"
CT2_ROCM_URL="https://github.com/OpenNMT/CTranslate2/releases/download/v4.7.1/rocm-python-wheels-Linux.zip"

# helm/templates/*-model-init-job.yaml + deploy/examples/*/values.yaml
COSYTTS_MODEL_ID="FunAudioLLM/Fun-CosyVoice3-0.5B-2512"   # modelscope
EMAGE_REPO_ID="H-Liu1997/emage_audio"                     # huggingface
EMOTION_REPO_ID="superb/hubert-large-superb-er"           # huggingface
WHISPER_REPO_PREFIX="Systran/faster-whisper"              # + -<size>

QDRANT_VERSION="v1.12.4"
QDRANT_IMAGE="qdrant/qdrant:$QDRANT_VERSION"

# =============================================================================
# Resource cost model
# =============================================================================
# All figures in MiB, steady-state, measured/estimated on the pinned versions.
# They are deliberately a little pessimistic: the point is to refuse an install
# that would OOM at the first utterance, not to squeeze the last megabyte.
#
#   TTS      CosyVoice3-0.5B: LM (0.5B) + flow-matching + HiFi vocoder + the
#            zero-shot prompt cache. fp16 on CUDA (the code enables it when
#            CUDA is present), fp32 otherwise.
#   GESTURE  EMAGE audio: 4 VQ-VAEs + global AE + the audio transformer, plus
#            the sliding-window activations.
#   STT      faster-whisper via CTranslate2, incl. beam/encoder buffers.
#   EMO      hubert-large prosody classifier — always CPU (the module never
#            moves tensors to a device), and it is instantiated *per session*.
#   LLM/EMB  Ollama, Q4_K_M weights + KV cache at the default context.

VRAM_TTS_FP16=4500
VRAM_TTS_FP32=7000
VRAM_GESTURE=2200
declare -A VRAM_STT=( [base]=1000 [small]=1600 [medium]=3200 [large-v3]=6000 )
declare -A RAM_STT=(  [base]=700  [small]=1100 [medium]=2400 [large-v3]=4500 )
RAM_TTS_CPU=6000
# Piper: a 61 MB ONNX voice plus onnxruntime arena. Measured ~0.03x realtime on
# a Ryzen 7840HS, i.e. ~30x faster than it speaks, so it never needs a GPU.
RAM_TTS_PIPER=400
DISK_PIPER=200          # voice model + piper-tts wheel
RAM_GESTURE_CPU=2500
RAM_EMO_PER_SESSION=1600
RAM_BASE=3000          # ray head + serve controller + proxy + HuRI actor

# LLM tiers, largest first: "<ollama tag>|<VRAM MiB>|<RAM MiB on CPU>"
LLM_TIERS=(
  "qwen2.5:14b|9500|11000"
  "mistral:7b|5500|6500"
  "llama3.2:3b|2800|3600"
  "qwen2.5:1.5b|1600|2200"
)
EMBED_MODEL_DEFAULT="bge-m3"
VRAM_EMBED=1300

# Rough on-disk footprint (MiB) used for the free-space check.
DISK_VENV_BASE=2500
DISK_TORCH_CUDA=5000
DISK_TORCH_ROCM=7000
DISK_TORCH_CPU=900
DISK_COSYVOICE=6000     # repo + submodules + model snapshot
DISK_EMAGE=1500
DISK_EMOTION=1300
declare -A DISK_STT=( [base]=200 [small]=600 [medium]=1800 [large-v3]=3300 )
DISK_SERVICES=3000      # qdrant image/binary + ollama runtime

# =============================================================================
# Options
# =============================================================================

DRY_RUN=0
PLAN_ONLY=0
ASSUME_YES=0
PROFILE="auto"
GPU_INDEX=0
VRAM_OVERRIDE=""
RESERVE_VRAM=700
STT_SIZE="base"
LLM_MODEL=""
EMBED_MODEL="$EMBED_MODEL_DEFAULT"
LLM_URL="http://localhost:11434"
LLM_PROVIDER=""
LLM_API_KEY=""
EMBED_URL=""
QDRANT_URL="http://localhost:6333"
VERIFY_SSL=1
MODULES_OVERRIDE=""
STT_DEVICE_OVERRIDE=""
TTS_ENGINE="auto"
PIPER_VOICE_NAME="en_US-lessac-medium"
VOICE_SAMPLE=""
VOICE_TRANSCRIPT="Instinct creates its own oppressors and bids us rise up against them."
PYTHON_BIN=""
FORCE_TTS=0
FORCE_GESTURE=0
FORCE_EMO=0
SKIP_SYSTEM=0
SKIP_PYTHON=0
SKIP_MODELS=0
SKIP_SERVICES=0
ONLY_STAGES=""

usage() {
  cat <<'USAGE'
HuRI local installer — installs the full stack on this machine (no Kubernetes).

  scripts/install_local.sh [options]

Planning
  -n, --dry-run           Print every command instead of running it.
      --plan-only         Probe the machine, print the capability plan, exit.
      --profile P         auto | nvidia | amd | cpu       (default: auto)
      --gpu-index N       Which GPU to budget against     (default: 0)
      --vram MB           Override detected VRAM (for GPUs the tools can't read)
      --reserve-vram MB   VRAM left free for driver/context (default: 700)
      --stt-model SIZE    base | small | medium | large-v3 (default: base)
      --tts-engine E      auto | piper | cosyvoice         (default: auto)
                          auto: cosyvoice when an NVIDIA GPU has room for it,
                            piper everywhere else.
                          piper: ONNX, ~30x faster than realtime on any CPU,
                            61 MB voice, no GPU — works on every machine.
                          cosyvoice: zero-shot voice cloning from --voice-sample,
                            but needs an NVIDIA GPU (on CPU it is ~7x SLOWER
                            than realtime; on ROCm the vocoder crashes).
      --piper-voice NAME  Piper voice to download (default: en_US-lessac-medium)
                          Browse: https://huggingface.co/rhasspy/piper-voices
      --llm-model TAG     Model name; default is an Ollama tag picked from the
                          free VRAM/RAM. Required with a remote --llm-url.
      --embed-model TAG   Embedding model (default: bge-m3)

Remote endpoints (anything not on localhost is treated as already running:
no Ollama/Qdrant is installed for it, and it costs no local VRAM/RAM)
      --llm-url URL       LLM base URL, http(s)  (default: http://localhost:11434)
      --llm-provider P    ollama | vllm | api    (default: inferred from the URL
                          and whether an API key was given)
      --llm-api-key KEY   Bearer token. Stored in .huri-local/secrets.env (0600)
                          and exported as HURI_LLM_API_KEY — never written into
                          the generated config.
      --embed-url URL     Embeddings base URL    (default: same as --llm-url)
      --qdrant-url URL    Qdrant URL             (default: http://localhost:6333)
      --no-verify-ssl     Skip TLS verification (self-signed internal endpoints)
      --force-tts         Keep TTS even when the plan puts it on CPU
      --force-gesture     Keep gesture generation even when it lands on CPU
      --force-emo         Keep prosody/emotion even when RAM is tight
      --modules LIST      Explicit module allow-list instead of the planned one,
                          e.g. --modules vad,stt,tag,qag,rag  (text pipeline) or
                          --modules rag (text in, text out). Subset of:
                          vad,stt,tag,emo,eag,qag,rag,tts,mov
      --stt-device D      gpu | cpu — override where speech-to-text runs

Install
  -y, --yes               Don't ask for confirmation
      --python PATH       Interpreter used to create the venv (needs 3.10–3.12)
      --venv PATH         Virtualenv location            (default: ./.venv)
      --voice-sample PATH Reference voice sample for TTS -> assets/voice.wav
      --voice-transcript T  Its exact transcript (CosyVoice3 zero-shot prompt)
      --skip-system       Don't touch system packages (apt/dnf/pacman)
      --skip-python       Don't create/populate the virtualenv
      --skip-models       Don't download model weights
      --skip-services     Don't install/start Qdrant and Ollama
      --only S[,S...]     Run only these stages:
                          system,python,models,services,config,verify
  -h, --help              This message

Outputs
  .huri-local/                    plan.env, env.sh, start.sh, stop.sh, status.sh
  config/huri_local.generated.yaml       Ray Serve config for this machine
  config/client_local.generated.yaml     matching client config
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run)      DRY_RUN=1 ;;
    --plan-only)       PLAN_ONLY=1 ;;
    -y|--yes)          ASSUME_YES=1 ;;
    --profile)         PROFILE="${2:?}"; shift ;;
    --gpu-index)       GPU_INDEX="${2:?}"; shift ;;
    --vram)            VRAM_OVERRIDE="${2:?}"; shift ;;
    --reserve-vram)    RESERVE_VRAM="${2:?}"; shift ;;
    --stt-model)       STT_SIZE="${2:?}"; shift ;;
    --modules)         MODULES_OVERRIDE="${2:?}"; shift ;;
    --stt-device)      STT_DEVICE_OVERRIDE="${2:?}"; shift ;;
    --tts-engine)      TTS_ENGINE="${2:?}"; shift ;;
    --piper-voice)     PIPER_VOICE_NAME="${2:?}"; shift ;;
    --llm-model)       LLM_MODEL="${2:?}"; shift ;;
    --embed-model)     EMBED_MODEL="${2:?}"; shift ;;
    --llm-url)         LLM_URL="${2:?}"; shift ;;
    --llm-provider)    LLM_PROVIDER="${2:?}"; shift ;;
    --llm-api-key)     LLM_API_KEY="${2:?}"; shift ;;
    --embed-url)       EMBED_URL="${2:?}"; shift ;;
    --qdrant-url)      QDRANT_URL="${2:?}"; shift ;;
    --no-verify-ssl)   VERIFY_SSL=0 ;;
    --voice-sample)    VOICE_SAMPLE="${2:?}"; shift ;;
    --voice-transcript) VOICE_TRANSCRIPT="${2:?}"; shift ;;
    --python)          PYTHON_BIN="${2:?}"; shift ;;
    --venv)            VENV_DIR="$(readlink -f "${2:?}")"; shift ;;
    --force-tts)       FORCE_TTS=1 ;;
    --force-gesture)   FORCE_GESTURE=1 ;;
    --force-emo)       FORCE_EMO=1 ;;
    --skip-system)     SKIP_SYSTEM=1 ;;
    --skip-python)     SKIP_PYTHON=1 ;;
    --skip-models)     SKIP_MODELS=1 ;;
    --skip-services)   SKIP_SERVICES=1 ;;
    --only)            ONLY_STAGES="${2:?}"; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -n "${VRAM_STT[$STT_SIZE]:-}" ]] || { echo "unknown --stt-model '$STT_SIZE'" >&2; exit 2; }
case "$PROFILE" in auto|nvidia|amd|cpu) ;; *) echo "unknown --profile '$PROFILE'" >&2; exit 2 ;; esac
case "${STT_DEVICE_OVERRIDE:-cpu}" in gpu|cpu) ;; *) echo "unknown --stt-device '$STT_DEVICE_OVERRIDE' (gpu|cpu)" >&2; exit 2 ;; esac
case "$TTS_ENGINE" in auto|piper|cosyvoice) ;; *) echo "unknown --tts-engine '$TTS_ENGINE' (auto|piper|cosyvoice)" >&2; exit 2 ;; esac

# An API key given on an earlier run lives in .huri-local/secrets.env; reload it
# so `--only config` re-runs keep working without re-passing the secret.
if [[ -z "$LLM_API_KEY" && -r "$STATE_DIR/secrets.env" ]]; then
  # shellcheck disable=SC1091
  source "$STATE_DIR/secrets.env"
  LLM_API_KEY="${HURI_LLM_API_KEY:-}"
fi

# rag.py falls back to llm_url when embedding_url is empty; mirror that here so
# the plan and the generated config agree on which endpoint is used.
EMBED_URL_EFF="${EMBED_URL:-$LLM_URL}"

# Port from --qdrant-url, so the flag reaches the code paths that provision and
# probe Qdrant instead of only the value written into the generated config.
# Anchored to the authority component: a loose `:([0-9]+)` would match a port
# inside a path (…/collections/v2 -> 2). Falls back to Qdrant's default.
QDRANT_PORT="$(printf '%s' "$QDRANT_URL" \
  | sed -nE 's#^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*:([0-9]+).*#\1#p')"
QDRANT_PORT="${QDRANT_PORT:-6333}"
# Qdrant's gRPC port conventionally sits directly above the HTTP one.
QDRANT_GRPC_PORT=$(( QDRANT_PORT + 1 ))

if [[ -z "$LLM_PROVIDER" ]]; then
  # /api/chat (ollama) vs /v1/chat/completions (vllm, api). Only "api" sends the
  # Authorization header, so a key implies it. See rag.py::_llm_stream.
  if   [[ "$LLM_URL" == *:11434* ]]; then LLM_PROVIDER="ollama"
  elif [[ -n "$LLM_API_KEY" ]];    then LLM_PROVIDER="api"
  else                                  LLM_PROVIDER="vllm"
  fi
fi
case "$LLM_PROVIDER" in ollama|vllm|api) ;; *) echo "unknown --llm-provider '$LLM_PROVIDER'" >&2; exit 2 ;; esac

# =============================================================================
# Output helpers
# =============================================================================

if [[ -t 1 ]]; then
  C_RST=$'\033[0m'; C_B=$'\033[1m'; C_DIM=$'\033[2m'
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_C=$'\033[36m'
else
  C_RST=; C_B=; C_DIM=; C_R=; C_G=; C_Y=; C_C=
fi

_log()  { [[ -d "$STATE_DIR" ]] && printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG_FILE" || true; }
info()  { printf '%s\n' "$*"; _log "INFO  $*"; }
ok()    { printf '  %s✓%s %s\n' "$C_G" "$C_RST" "$*"; _log "OK    $*"; }
warn()  { printf '  %s!%s %s\n' "$C_Y" "$C_RST" "$*"; _log "WARN  $*"; }
err()   { printf '  %s✗%s %s\n' "$C_R" "$C_RST" "$*" >&2; _log "ERR   $*"; }
die()   { err "$*"; exit 1; }
step()  { printf '\n%s==>%s %s%s%s\n' "$C_C" "$C_RST" "$C_B" "$*" "$C_RST"; _log "STEP  $*"; }
note()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RST"; }

# The command run() is currently executing, for the ERR trap. Inside run() the
# trap cannot use $BASH_COMMAND (it expands to the literal string '"$@"') nor
# $LINENO (it always resolves to run()'s own body), so we record it explicitly.
LAST_RUN=""

# run <cmd...> — echo in dry-run, execute otherwise (stdout/stderr also logged).
run() {
  if (( DRY_RUN )); then
    printf '    %s$ %s%s\n' "$C_DIM" "$(printf '%q ' "$@")" "$C_RST"
    return 0
  fi
  LAST_RUN="$*"
  _log "RUN   $*"
  local rc=0
  if [[ -d "$STATE_DIR" ]]; then
    # Tee into install.log: without this, pip/curl/pacman error text is lost and
    # a failure is undiagnosable afterwards. errexit would abort on the pipeline
    # before PIPESTATUS could be read, hence the set +e / set -e pair.
    set +e
    "$@" 2>&1 | tee -a "$LOG_FILE"
    rc=${PIPESTATUS[0]}
    set -e
  else
    set +e; "$@"; rc=$?; set -e
  fi
  if (( rc == 0 )); then LAST_RUN=""; fi
  return "$rc"
}

# Write a file (respecting --dry-run). Content on stdin.
write_file() {
  local path="$1"
  if (( DRY_RUN )); then
    printf '    %s$ write %s%s\n' "$C_DIM" "$path" "$C_RST"
    cat >/dev/null
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  cat >"$path"
  _log "WRITE $path"
}

have() { command -v "$1" >/dev/null 2>&1; }

# is_local_url <url> — true when the URL points at this machine, i.e. when the
# installer is responsible for providing the service behind it.
is_local_url() {
  local host
  host="$(printf '%s' "$1" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#^[^/@]*@##; s#[/?].*##; s#:[0-9]+$##; s#^\[|\]$##g')"
  case "$host" in
    localhost|127.0.0.1|0.0.0.0|::1|"$(hostname)"|"$(hostname -s 2>/dev/null)") return 0 ;;
    *) return 1 ;;
  esac
}

confirm() {
  local prompt="$1"
  (( ASSUME_YES )) && return 0
  (( DRY_RUN )) && return 0
  local reply
  read -r -p "$prompt [y/N] " reply || true
  [[ "$reply" == [yY]* ]]
}

stage_enabled() {
  [[ -z "$ONLY_STAGES" ]] && return 0
  [[ ",$ONLY_STAGES," == *",$1,"* ]]
}

mb_to_gb() { awk -v m="$1" 'BEGIN{printf "%.1f", m/1024}'; }

# --- ROCm helpers ------------------------------------------------------------

# The pip wheels this installer pulls (repo.radeon.com torch/torchaudio/triton
# and the OpenNMT CTranslate2 build) link the ROCm userspace as hard DT_NEEDED
# entries and bundle none of it. Without these packages every `import torch` and
# `import ctranslate2` dies with "libamdhip64.so.7: cannot open shared object
# file". deploy/Dockerfile.amd:18-36 installs the same set inside the image.
ROCM_PKGS_PACMAN=(rocm-hip-runtime rocm-hip-libraries roctracer hipblaslt
                  hipsparselt miopen-hip rccl)
ROCM_PKGS_APT=(rocm-hip-runtime rocm-hip-libraries rocm-smi-lib rocblas hipblas
               hipblaslt hipfft hiprand hipsolver hipsparse rocsolver
               miopen-hip rccl roctracer)

# rocm_have_runtime — is the HIP runtime the wheels need actually present?
rocm_have_runtime() {
  ldconfig -p 2>/dev/null | grep -q 'libamdhip64\.so\.7' && return 0
  [[ -e "${ROCM_PATH:-/opt/rocm}/lib/libamdhip64.so.7" ]]
}

# rocm_will_be_available — present already, or about to be installed by the
# system stage. Only pacman and apt are covered; elsewhere the ROCm package
# names differ too much to guess, so the plan degrades to CPU instead.
rocm_will_be_available() {
  rocm_have_runtime && return 0
  (( SKIP_SYSTEM )) && return 1
  stage_enabled system || return 1
  case "$PKG_MGR" in pacman|apt) return 0 ;; *) return 1 ;; esac
}

rocm_runtime_hint() {
  case "$PKG_MGR" in
    pacman) note "sudo pacman -S --needed ${ROCM_PKGS_PACMAN[*]}" ;;
    apt)    note "add the repo.radeon.com apt source for ROCm $ROCM_VERSION, then:"
            note "sudo apt install ${ROCM_PKGS_APT[*]}" ;;
    *)      note "install the ROCm $ROCM_VERSION runtime for your distro (hip-runtime + rocblas/hipblas/miopen/rccl/roctracer)" ;;
  esac
  note "Then re-run: scripts/install_local.sh --only verify"
}

# rocm_gfx_override <venv python> — echo the HSA_OVERRIDE_GFX_VERSION this GPU
# needs, or nothing if it is natively supported.
#
# A ROCm torch wheel only carries code objects for the architectures it was
# built for. AMD's repo.radeon.com ".lw." (lightweight) wheels target data
# centre parts and omit consumer RDNA3 — notably gfx1102 (RX 7600/7700S). On
# such a card torch.cuda.is_available() is True and the device name resolves,
# but every kernel launch aborts inside HIP with
#   hip_code_object.cpp:400: Assertion `err == hipSuccess' failed
# which looks like a hardware fault and is really a missing binary. Reporting a
# same-family supported arch (gfx1102 -> 11.0.0, i.e. gfx1100) makes the
# prebuilt kernels load. Same RDNA3 ISA, different CU counts.
rocm_gfx_override() {
  local vpy="$1" arch supported
  arch="$("$vpy" -c 'import torch;print(torch.cuda.get_device_properties(0).gcnArchName.split(":")[0])' 2>/dev/null)" || return 0
  [[ -n "$arch" ]] || return 0
  supported="$("$vpy" -c 'import torch;print(",".join(torch.cuda.get_arch_list()))' 2>/dev/null)" || return 0
  [[ ",$supported," == *",$arch,"* ]] && return 0
  case "$arch" in
    gfx1102|gfx1103|gfx1150|gfx1151) [[ ",$supported," == *",gfx1100,"* ]] && echo "11.0.0" ;;
    gfx1031|gfx1032|gfx1034|gfx1035) [[ ",$supported," == *",gfx1030,"* ]] && echo "10.3.0" ;;
  esac
}

# --- STT device vocabulary ---------------------------------------------------
# The plan speaks in gpu/cpu; faster-whisper only accepts cpu | cuda | auto
# (CTranslate2 calls the ROCm backend "cuda" too). Translate once, here, so the
# generated config and the verification probe cannot disagree.
stt_ct2_device()       { [[ "$P_STT_DEV" == "gpu" ]] && echo cuda    || echo cpu;  }
stt_ct2_compute_type() { [[ "$P_STT_DEV" == "gpu" ]] && echo float16 || echo int8; }

# The ERR trap fires twice for a failing run(): once inside the function, then
# again as errexit propagates to the top level (where BASH_LINENO is 0). Report
# the first one only, which is the one carrying the real caller line.
ERR_REPORTED=0
_on_err() {
  (( ERR_REPORTED )) && return 0
  ERR_REPORTED=1
  err "failed: ${LAST_RUN:-$BASH_COMMAND} (${BASH_SOURCE[0]##*/}:${1:-?}, see $LOG_FILE)"
}
trap '_on_err "${BASH_LINENO[0]}"' ERR

# =============================================================================
# 1. Detection
# =============================================================================

OS_NAME=""; OS_VERSION=""; PKG_MGR=""; IS_WSL=0
CPU_CORES=0; RAM_TOTAL_MB=0; RAM_FREE_MB=0; DISK_FREE_MB=0
GPU_VENDOR="none"; GPU_NAME=""; GPU_COUNT=0; GPU_VRAM_MB=0; GPU_DRIVER=""
# HSA_OVERRIDE_GFX_VERSION for this GPU, or empty when natively supported.
GFX_OVERRIDE=""
PY_BIN=""; PY_VERSION=""
HAS_DOCKER=""; HAS_OLLAMA=0

detect_os() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    OS_NAME="$(. /etc/os-release && echo "${ID:-unknown}")"
    OS_VERSION="$(. /etc/os-release && echo "${VERSION_ID:-}")"
  else
    OS_NAME="$(uname -s)"
  fi
  grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && IS_WSL=1 || true
  if   have apt-get; then PKG_MGR="apt"
  elif have dnf;     then PKG_MGR="dnf"
  elif have pacman;  then PKG_MGR="pacman"
  elif have zypper;  then PKG_MGR="zypper"
  else PKG_MGR=""
  fi
}

detect_host() {
  CPU_CORES="$(nproc 2>/dev/null || echo 1)"
  RAM_TOTAL_MB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 ))
  RAM_FREE_MB=$(( $(awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 ))
  DISK_FREE_MB=$(( $(df -Pk "$REPO_ROOT" 2>/dev/null | awk 'NR==2{print $4}' || echo 0) / 1024 ))
}

detect_nvidia() {
  have nvidia-smi || return 1
  local out
  out="$(nvidia-smi --query-gpu=index,name,memory.total,driver_version \
        --format=csv,noheader,nounits 2>/dev/null)" || return 1
  [[ -n "$out" ]] || return 1
  GPU_COUNT="$(printf '%s\n' "$out" | grep -c .)"
  local line
  line="$(printf '%s\n' "$out" | awk -F', *' -v i="$GPU_INDEX" '$1+0==i{print;exit}')"
  [[ -n "$line" ]] || die "no NVIDIA GPU with index $GPU_INDEX (found $GPU_COUNT)"
  GPU_VENDOR="nvidia"
  GPU_NAME="$(awk -F', *' '{print $2}' <<<"$line")"
  GPU_VRAM_MB="$(awk -F', *' '{print int($3)}' <<<"$line")"
  GPU_DRIVER="$(awk -F', *' '{print $4}' <<<"$line")"
  return 0
}

detect_amd() {
  local bytes="" smi=""
  # Arch installs rocm-smi to /opt/rocm/bin, which is not on PATH by default, so
  # `have rocm-smi` failed and detection silently degraded to the lspci path.
  if ! have rocm-smi && [[ -x "${ROCM_PATH:-/opt/rocm}/bin/rocm-smi" ]]; then
    PATH="${ROCM_PATH:-/opt/rocm}/bin:$PATH"
    export PATH
  fi
  # The old amd-smi branch took the first '"total"|"size": N' integer anywhere in
  # `amd-smi static --json`, which can be a cache, partition or BAR size rather
  # than the framebuffer, and is reported in MB by some releases while the
  # arithmetic below assumes bytes. rocm-smi's output is parseable per device;
  # anything else falls through to lspci + --vram.
  if have rocm-smi; then
    smi="$(rocm-smi --showmeminfo vram 2>/dev/null || true)"
    # One "VRAM Total Memory" line per device — that is the device count.
    GPU_COUNT="$(printf '%s\n' "$smi" | grep -ciE 'vram total memory' || true)"
    [[ "$GPU_COUNT" =~ ^[0-9]+$ ]] || GPU_COUNT=0
    bytes="$(printf '%s\n' "$smi" \
      | grep -iE "^GPU\[$GPU_INDEX\][^0-9]*vram total memory" \
      | grep -oE '[0-9]{7,}' | head -1 || true)"
    if [[ -z "$bytes" ]] && (( GPU_COUNT > 0 )); then
      die "no AMD GPU with index $GPU_INDEX — rocm-smi reports $GPU_COUNT device(s); use --gpu-index 0..$(( GPU_COUNT - 1 ))"
    fi
  fi
  if [[ -z "$bytes" ]]; then
    # No ROCm tooling: is there an AMD display/compute device at all?
    have lspci && lspci 2>/dev/null | grep -qiE 'VGA|3D|Display' \
      && lspci 2>/dev/null | grep -iE 'VGA|3D|Display' | grep -qi 'AMD/ATI' || return 1
    GPU_VENDOR="amd"
    GPU_COUNT="$(lspci 2>/dev/null | grep -iE 'VGA|3D|Display' | grep -ci 'AMD/ATI' || true)"
    [[ "$GPU_COUNT" =~ ^[0-9]+$ ]] && (( GPU_COUNT > 0 )) || GPU_COUNT=1
    GPU_NAME="$(lspci 2>/dev/null | grep -iE 'VGA|3D|Display' | grep -i 'AMD/ATI' \
      | sed -n "$(( GPU_INDEX + 1 ))p" | cut -d':' -f3- | sed 's/^ //')"
    [[ -n "$GPU_NAME" ]] || GPU_NAME="AMD GPU"
    GPU_VRAM_MB=0
    return 0
  fi
  GPU_VENDOR="amd"
  GPU_VRAM_MB=$(( bytes / 1024 / 1024 ))
  # rocm-smi prints:  GPU[0]<tab><tab>: Card Series: <tab><tab>AMD Radeon RX 7700S
  # The value therefore starts at the *third* colon-separated field; -f2- kept
  # the "Card Series:" label and its tabs, which is how HURI_GPU_NAME ended up
  # as 'Card Series: \t\tAMD Radeon RX 7700S' in plan.env.
  GPU_NAME="$(rocm-smi --showproductname 2>/dev/null \
    | grep -iE "^GPU\[$GPU_INDEX\][^:]*:[[:space:]]*card series" \
    | head -1 | cut -d':' -f3- | tr -s ' \t' ' ' | sed 's/^ *//; s/ *$//' || true)"
  [[ -n "$GPU_NAME" ]] || GPU_NAME="AMD GPU"
  return 0
}

detect_gpu() {
  case "$PROFILE" in
    cpu)    GPU_VENDOR="none"; return ;;
    nvidia) detect_nvidia || die "--profile nvidia but nvidia-smi reports no usable GPU" ;;
    amd)    detect_amd    || die "--profile amd but no AMD GPU found (install rocm-smi, or pass --vram)" ;;
    auto)   detect_nvidia || detect_amd || GPU_VENDOR="none" ;;
  esac
  if [[ -n "$VRAM_OVERRIDE" ]]; then
    GPU_VRAM_MB="$VRAM_OVERRIDE"
    # An `x && y` as the last statement of a function returns 1 when x is false,
    # which under errexit aborted the whole installer whenever --vram was passed
    # on a machine where a GPU *was* detected. Hence the explicit if + return 0.
    if [[ "$GPU_VENDOR" == "none" ]]; then GPU_VENDOR="$PROFILE"; fi
  fi
  return 0
}

detect_python() {
  local candidates=()
  [[ -n "$PYTHON_BIN" ]] && candidates+=("$PYTHON_BIN")
  [[ -x "$VENV_DIR/bin/python" ]] && candidates+=("$VENV_DIR/bin/python")
  candidates+=(python3.12 python3.11 python3.10 python3)
  local c v major minor
  for c in "${candidates[@]}"; do
    have "$c" || [[ -x "$c" ]] || continue
    v="$("$c" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)" || continue
    major="${v%%.*}"; minor="$(cut -d. -f2 <<<"$v")"
    if [[ "$major" == 3 ]] && (( minor >= 10 && minor <= 12 )); then
      PY_BIN="$(command -v "$c" || echo "$c")"; PY_VERSION="$v"; return 0
    fi
  done
  return 1
}

detect_tools() {
  if have docker && docker info >/dev/null 2>&1; then HAS_DOCKER="docker"
  elif have podman; then HAS_DOCKER="podman"
  else HAS_DOCKER=""; fi
  have ollama && HAS_OLLAMA=1 || HAS_OLLAMA=0
}

report_detection() {
  step "Machine"
  printf '    %-14s %s %s\n' "os"     "$OS_NAME" "$OS_VERSION$( ((IS_WSL)) && echo ' (WSL2)')"
  printf '    %-14s %s cores, %s GiB RAM (%s GiB available)\n' "cpu/ram" \
    "$CPU_CORES" "$(mb_to_gb "$RAM_TOTAL_MB")" "$(mb_to_gb "$RAM_FREE_MB")"
  printf '    %-14s %s GiB free at %s\n' "disk" "$(mb_to_gb "$DISK_FREE_MB")" "$REPO_ROOT"
  if [[ "$GPU_VENDOR" == "none" ]]; then
    printf '    %-14s %snone detected%s — CPU-only plan\n' "gpu" "$C_Y" "$C_RST"
  else
    printf '    %-14s %s x%s, %s GiB VRAM%s\n' "gpu" \
      "${GPU_NAME:-$GPU_VENDOR}" "${GPU_COUNT:-1}" "$(mb_to_gb "$GPU_VRAM_MB")" \
      "${GPU_DRIVER:+, driver $GPU_DRIVER}"
  fi
  printf '    %-14s %s\n' "python" "${PY_BIN:-<none suitable>} ${PY_VERSION:+($PY_VERSION)}"
  compute_system_packages
  if [[ -n "$PKG_MGR" ]]; then
    printf '    %-14s %s\n' "pkg manager" "$PKG_MGR"
    printf '    %-14s %s\n' "packages" "${SYS_PKGS[*]:-<none>}"
  else
    printf '    %-14s %s\n' "pkg manager" "${C_Y}none detected${C_RST} — install a C toolchain, ffmpeg, libsndfile and portaudio yourself"
  fi
  printf '    %-14s %s\n' "container" "${HAS_DOCKER:-none}"
  printf '    %-14s %s\n' "ollama" "$( ((HAS_OLLAMA)) && echo installed || echo 'not installed')"

  if [[ "$GPU_VENDOR" == "amd" && $IS_WSL == 1 ]]; then
    warn "ROCm is not usable under WSL2 — planning as CPU-only."
    GPU_VENDOR="none"
  fi
  if [[ "$GPU_VENDOR" != "none" && "$GPU_VRAM_MB" -eq 0 ]]; then
    if [[ "$PROFILE" == "auto" ]]; then
      warn "found a $GPU_VENDOR GPU but no tooling reports its VRAM — planning as CPU-only."
      note "Install the vendor tools (rocm-smi / nvidia-smi), or re-run with --vram <MiB>."
      GPU_VENDOR="none"
    else
      die "found a $GPU_VENDOR GPU but could not read its VRAM — re-run with --vram <MiB>"
    fi
  fi
}

# =============================================================================
# 2. Planning — what can this machine actually run?
# =============================================================================

# Per-component decisions filled in by plan().
P_STT_DEV=""; P_STT_WHY=""
P_TTS_DEV=""; P_TTS_WHY=""
P_GES_DEV=""; P_GES_WHY=""
P_EMO_DEV=""; P_EMO_WHY=""
P_LLM_DEV=""; P_LLM_WHY=""
P_TTS_VRAM=0; P_GES_VRAM=0; P_STT_VRAM=0; P_LLM_VRAM=0; P_LLM_RAM=0
P_VERDICT=""; P_MODULES=""; P_RAM_NEED=0; P_DISK_NEED=0
P_EMBED_DEV=""; LLM_REMOTE=0; EMBED_REMOTE=0; QDRANT_REMOTE=0; NEED_OLLAMA=0
P_TTS_FRAC="0"; P_GES_FRAC="0"; P_STT_FRAC="0"; P_RAY_GPUS=0

# frac <mb> — that component's share of the GPU, as a Ray num_gpus fraction
# rounded up to 2 decimals (Ray fractions are a scheduling/packing hint).
# frac_ceil <mb> — round up: used for HURI_GESTURE_GPU_MEM_FRACTION, a hard
# allocation cap, where rounding down would starve the model it is sizing.
frac_ceil() {
  awk -v m="$1" -v t="$GPU_VRAM_MB" 'BEGIN{
    if (t <= 0) { print "0"; exit }
    f = (int(m * 100 / t) + 1) / 100
    if (f > 1) f = 1
    printf "%.2f", f
  }'
}

frac() {
  awk -v m="$1" -v t="$GPU_VRAM_MB" 'BEGIN{
    if (t <= 0) { print "0"; exit }
    f = int(m * 100 / t) / 100      # floor: the shares already sum to < 1
    if (f < 0.01) f = 0.01
    if (f > 1)    f = 1
    printf "%.2f", f
  }'
}

# Which endpoints this machine has to host itself.
resolve_endpoints() {
  is_local_url "$LLM_URL"      && LLM_REMOTE=0    || LLM_REMOTE=1
  is_local_url "$EMBED_URL_EFF" && EMBED_REMOTE=0 || EMBED_REMOTE=1
  is_local_url "$QDRANT_URL"   && QDRANT_REMOTE=0 || QDRANT_REMOTE=1
  NEED_OLLAMA=0
  (( LLM_REMOTE ))   || NEED_OLLAMA=1
  (( EMBED_REMOTE )) || NEED_OLLAMA=1
  if (( LLM_REMOTE )) && [[ -z "$LLM_MODEL" ]]; then
    die "--llm-url points at $LLM_URL — pass --llm-model <name as the endpoint serves it>"
  fi
  # Only the "api" provider attaches the bearer token (rag.py::_llm_stream), so
  # a key with any other provider would silently never be sent.
  if [[ -n "$LLM_API_KEY" && "$LLM_PROVIDER" != "api" ]]; then
    warn "an API key is set but --llm-provider is '$LLM_PROVIDER', which sends no"
    note "Authorization header. Use --llm-provider api if the endpoint needs the key."
  fi
}

plan() {
  local plan_tts_done=0
  # An AMD GPU is only usable if the ROCm userspace is there — the wheels link
  # it dynamically and bundle none of it. Decided here, at the top of the
  # planner, so that every stage agrees: planning as "amd" and then discovering
  # the truth during pip is what produced a plan promising GPU STT on a machine
  # where `import ctranslate2` could not even succeed.
  if [[ "$GPU_VENDOR" == "amd" ]] && ! rocm_will_be_available; then
    warn "AMD GPU found, but the ROCm $ROCM_VERSION runtime is missing and this run will not install it"
    note "planning as CPU-only. To use the GPU, install the runtime and re-run:"
    rocm_runtime_hint
    GPU_VENDOR="none"
    GPU_VRAM_MB=0
  fi

  local pool=0
  if [[ "$GPU_VENDOR" != "none" ]]; then
    pool=$(( GPU_VRAM_MB - RESERVE_VRAM ))
    (( pool < 0 )) && pool=0
  fi

  # --- TTS (CosyVoice3) ------------------------------------------------------
  # ROCm stays off the GPU, but NOT for the reason requirements-amd.txt gives
  # ("CosyVoice runs on the NVIDIA worker only"). Measured on gfx1102 (RX 7700S,
  # ROCm 7.2, torch 2.8+rocm6.4 — a build that DOES contain gfx1102 kernels):
  #   * CosyVoice3 imports and loads fine, and the LLM stage runs on the GPU;
  #   * synthesis then segfaults in the HiFi-GAN vocoder's f0_predictor, inside
  #     a MIOpen Conv1d (cosyvoice/hifigan/f0_predictor.py -> transformer/
  #     convolution.py -> torch conv) — with fp16 AND fp32, with stream=True and
  #     stream=False, and with MIOPEN_DEBUG_CONV_IMPLICIT_GEMM=0 /
  #     MIOPEN_FIND_MODE=NORMAL / MIOPEN_DEBUG_CONV_WINOGRAD=0.
  # It is a MIOpen convolution gap on RDNA3, not a packaging choice. CPU works
  # (~7x slower than realtime for a 0.5B model), so --force-tts lands there.
  # Re-test on a newer ROCm before flipping this to gpu.
  # Piper needs no GPU and is faster than realtime on any CPU, so it is simply
  # always on — this is what makes "voice out" work on every machine rather than
  # only on NVIDIA hosts.
  # "auto" takes CosyVoice (voice cloning) only where it actually runs well — an
  # NVIDIA GPU with room for it — and falls back to piper everywhere else.
  local tts_auto=0
  if [[ "$TTS_ENGINE" == "auto" ]]; then
    tts_auto=1
    if [[ "$GPU_VENDOR" == "nvidia" ]] && (( pool >= VRAM_TTS_FP16 )); then
      TTS_ENGINE="cosyvoice"
    else
      TTS_ENGINE="piper"
    fi
  fi
  if [[ "$TTS_ENGINE" == "piper" ]]; then
    P_TTS_DEV="cpu"
    P_TTS_WHY="piper (onnx) — ~30x faster than realtime, no GPU needed"
    (( tts_auto )) && P_TTS_WHY="auto: no NVIDIA GPU with $(mb_to_gb $VRAM_TTS_FP16) GiB free — piper (onnx), no GPU needed"
    plan_tts_done=1
  fi

  local tts_cost=$VRAM_TTS_FP16
  if (( ${plan_tts_done:-0} )); then
    :
  elif [[ "$GPU_VENDOR" == "nvidia" ]] && (( pool >= tts_cost )); then
    P_TTS_DEV="gpu"; P_TTS_VRAM=$tts_cost; pool=$(( pool - tts_cost ))
    P_TTS_WHY="fp16 on $GPU_NAME"
    (( tts_auto )) && P_TTS_WHY="auto: fp16 on $GPU_NAME (--tts-engine piper to keep the VRAM)"
  elif [[ "$GPU_VENDOR" == "amd" ]]; then
    if (( FORCE_TTS )); then
      P_TTS_DEV="cpu"; P_TTS_WHY="forced onto CPU; ROCm vocoder crashes in MIOpen conv (~7x realtime)"
    else
      P_TTS_DEV="off"; P_TTS_WHY="ROCm vocoder crashes in MIOpen conv (--force-tts runs it on CPU)"
    fi
  elif (( FORCE_TTS )); then
    P_TTS_DEV="cpu"; P_TTS_WHY="forced onto CPU — expect several seconds per sentence"
  else
    P_TTS_DEV="off"
    if [[ "$GPU_VENDOR" == "none" ]]; then
      P_TTS_WHY="no GPU; CPU synthesis is far slower than realtime (--force-tts to try)"
    else
      P_TTS_WHY="needs $(mb_to_gb $tts_cost) GiB VRAM, only $(mb_to_gb $pool) GiB left"
    fi
  fi

  # --- LLM (Ollama) ----------------------------------------------------------
  # Picked before gesture but *after* reserving gesture's slice when both fit:
  # a smaller LLM that keeps gestures beats a bigger one that kills them.
  local reserve_ges=0 tier tag tvram tram
  if [[ "$GPU_VENDOR" == "nvidia" ]] && (( pool >= VRAM_GESTURE + 1600 )); then
    reserve_ges=$VRAM_GESTURE
  fi
  # Embeddings only cost local VRAM when Ollama is the one serving them.
  local embed_cost=0
  (( EMBED_REMOTE )) || embed_cost=$VRAM_EMBED
  local llm_budget=$(( pool - reserve_ges - embed_cost ))
  (( llm_budget < 0 )) && llm_budget=0

  # RAM left for a CPU-served LLM once the rest of the stack is accounted for.
  # STT is charged at its CPU cost and EMO at one session even if they later land
  # elsewhere — over-reserving here only makes the choice safer.
  local cpu_other=$(( RAM_BASE + RAM_STT[$STT_SIZE] + RAM_EMO_PER_SESSION ))
  (( FORCE_TTS ))     && cpu_other=$(( cpu_other + RAM_TTS_CPU ))
  (( FORCE_GESTURE )) && cpu_other=$(( cpu_other + RAM_GESTURE_CPU ))
  local llm_ram_budget=$(( RAM_TOTAL_MB - cpu_other - 1024 ))   # 1 GiB margin
  (( llm_ram_budget < 0 )) && llm_ram_budget=0

  if (( LLM_REMOTE )); then
    # Someone else's GPU: no local budget, and the freed VRAM stays available
    # to TTS/gesture above.
    P_LLM_DEV="remote"
    P_LLM_WHY="$LLM_PROVIDER endpoint $LLM_URL"
    [[ -n "$LLM_API_KEY" ]] && P_LLM_WHY="$P_LLM_WHY (key from secrets.env)"
  elif [[ -n "$LLM_MODEL" ]]; then
    # User-chosen tag: honour it, budget with the closest known tier.
    tvram=5500; tram=6500
    for tier in "${LLM_TIERS[@]}"; do
      IFS='|' read -r tag tvram tram <<<"$tier"
      [[ "$tag" == "$LLM_MODEL" ]] && break
      tvram=5500; tram=6500
    done
    if [[ "$GPU_VENDOR" == "nvidia" ]] && (( llm_budget >= tvram )); then
      P_LLM_DEV="gpu"; P_LLM_VRAM=$tvram; pool=$(( pool - tvram - embed_cost ))
      P_LLM_WHY="user-selected, fits in VRAM"
    else
      P_LLM_DEV="cpu"; P_LLM_RAM=$tram
      P_LLM_WHY="user-selected; served from RAM by Ollama"
      (( tram > llm_ram_budget )) && P_LLM_WHY="user-selected; $(mb_to_gb $tram) GiB needed but only $(mb_to_gb $llm_ram_budget) GiB spare RAM"
    fi
  else
    for tier in "${LLM_TIERS[@]}"; do
      IFS='|' read -r tag tvram tram <<<"$tier"
      if [[ "$GPU_VENDOR" == "nvidia" ]] && (( llm_budget >= tvram )); then
        LLM_MODEL="$tag"; P_LLM_DEV="gpu"; P_LLM_VRAM=$tvram
        pool=$(( pool - tvram - embed_cost ))
        P_LLM_WHY="largest tier fitting the remaining VRAM"
        break
      fi
    done
    if [[ -z "$LLM_MODEL" ]]; then
      # CPU inference: pick by the RAM left after the rest of the stack.
      for tier in "${LLM_TIERS[@]}"; do
        IFS='|' read -r tag tvram tram <<<"$tier"
        if (( llm_ram_budget >= tram )); then
          LLM_MODEL="$tag"; P_LLM_DEV="cpu"; P_LLM_RAM=$tram
          P_LLM_WHY="no VRAM headroom; largest tier fitting $(mb_to_gb $llm_ram_budget) GiB spare RAM"
          break
        fi
      done
    fi
  fi
  # Embeddings: served by the same local Ollama, or by the remote endpoint.
  if (( EMBED_REMOTE )); then
    P_EMBED_DEV="remote"
  elif [[ "$GPU_VENDOR" == "nvidia" ]] && (( pool >= embed_cost )); then
    P_EMBED_DEV="gpu"
    (( LLM_REMOTE )) && pool=$(( pool - embed_cost ))   # not yet charged above
  else
    P_EMBED_DEV="cpu"
  fi

  if [[ -z "$LLM_MODEL" ]]; then
    LLM_MODEL="qwen2.5:1.5b"; P_LLM_DEV="off"
    if (( FORCE_TTS || FORCE_GESTURE )); then
      P_LLM_WHY="no RAM left: --force-tts/--force-gesture reserved it for CPU inference"
    else
      P_LLM_WHY="not enough RAM for any tier — RAG cannot answer"
    fi
  fi

  # --- Gesture (EMAGE) -------------------------------------------------------
  if [[ "$GPU_VENDOR" == "nvidia" ]] && (( pool >= VRAM_GESTURE )); then
    P_GES_DEV="gpu"; P_GES_VRAM=$VRAM_GESTURE; pool=$(( pool - VRAM_GESTURE ))
    P_GES_WHY="shares the GPU with TTS (memory fraction capped at runtime)"
  elif (( FORCE_GESTURE )); then
    P_GES_DEV="cpu"; P_GES_WHY="forced onto CPU — motion will lag the audio"
  else
    P_GES_DEV="off"
    if [[ "$GPU_VENDOR" == "nvidia" ]]; then
      P_GES_WHY="needs $(mb_to_gb $VRAM_GESTURE) GiB VRAM, only $(mb_to_gb $pool) GiB left"
    elif [[ "$GPU_VENDOR" == "amd" ]]; then
      P_GES_WHY="EMAGE is not part of the ROCm build (--force-gesture runs it on CPU)"
    else
      P_GES_WHY="no GPU; CPU inference is slower than realtime (--force-gesture to try)"
    fi
  fi

  # --- STT (faster-whisper) --------------------------------------------------
  # CPU int8 is genuinely realtime for base/small, so STT only takes GPU when
  # there is room left over. On ROCm it is the one component that *is* supported.
  local stt_cost="${VRAM_STT[$STT_SIZE]}"
  if [[ "$GPU_VENDOR" == "amd" ]] && (( pool >= stt_cost )); then
    P_STT_DEV="gpu"; P_STT_VRAM=$stt_cost; pool=$(( pool - stt_cost ))
    P_STT_WHY="CTranslate2 ROCm build"
  elif [[ "$GPU_VENDOR" == "nvidia" ]] && (( pool >= stt_cost )); then
    P_STT_DEV="gpu"; P_STT_VRAM=$stt_cost; pool=$(( pool - stt_cost ))
    P_STT_WHY="spare VRAM after TTS/LLM/gesture"
  else
    P_STT_DEV="cpu"; P_STT_WHY="int8 on $CPU_CORES cores — realtime for '$STT_SIZE'"
  fi

  # --- Emotion (hubert-large, CPU, per session) ------------------------------
  local emo_headroom=$(( RAM_TOTAL_MB - RAM_BASE - RAM_EMO_PER_SESSION ))
  if (( emo_headroom >= 2000 )) || (( FORCE_EMO )); then
    P_EMO_DEV="cpu"; P_EMO_WHY="hubert-large, ~$(mb_to_gb $RAM_EMO_PER_SESSION) GiB RAM per session"
  else
    P_EMO_DEV="off"; P_EMO_WHY="needs ~$(mb_to_gb $RAM_EMO_PER_SESSION) GiB RAM per session (--force-emo to keep)"
  fi

  # --- Module allow-list (HURI_MODULES) -------------------------------------
  local mods=("vad" "stt" "tag" "qag" "rag")
  [[ "$P_EMO_DEV" != "off" ]] && mods+=("emo" "eag")
  [[ "$P_TTS_DEV" != "off" ]] && mods+=("tts")
  [[ "$P_GES_DEV" != "off" && "$P_TTS_DEV" != "off" ]] && mods+=("mov")
  if [[ "$P_GES_DEV" != "off" && "$P_TTS_DEV" == "off" ]]; then
    P_GES_DEV="off"; P_GES_WHY="gesture is driven by TTS audio, which is disabled"
  fi
  P_MODULES="$(IFS=,; echo "${mods[*]}")"

  # --- Explicit overrides ----------------------------------------------------
  # Applied HERE: after the plan is formed but before the GPU fractions, RAM and
  # disk budgets below read these values. Applying them later would leave the
  # arithmetic computed for the device/module set the user just overrode.
  if [[ -n "$STT_DEVICE_OVERRIDE" ]]; then
    case "$STT_DEVICE_OVERRIDE" in
      gpu)
        [[ "$GPU_VENDOR" == "none" ]] && die "--stt-device gpu but no usable GPU was detected"
        P_STT_DEV="gpu"; P_STT_VRAM="${VRAM_STT[$STT_SIZE]}"
        P_STT_WHY="forced onto the GPU (--stt-device gpu)" ;;
      cpu)
        P_STT_DEV="cpu"; P_STT_VRAM=0
        P_STT_WHY="forced onto CPU (--stt-device cpu) — int8 on $CPU_CORES cores" ;;
    esac
  fi

  if [[ -n "$MODULES_OVERRIDE" ]]; then
    local known="vad stt tag emo eag qag rag tts mov" m bad=()
    local -a wanted=()
    IFS=',' read -r -a wanted <<<"$MODULES_OVERRIDE"
    for m in "${wanted[@]}"; do
      m="${m// /}"
      [[ -z "$m" ]] && continue
      [[ " $known " == *" $m "* ]] || bad+=("$m")
    done
    (( ${#bad[@]} )) && die "--modules lists unknown module(s): ${bad[*]}. Known: $known"
    P_MODULES="$(IFS=,; echo "${wanted[*]// /}")"
    # Keep the device plan consistent with the explicit list, so the budgets and
    # the generated deployments match what was asked for.
    if [[ ",$P_MODULES," != *",tts,"* && "$P_TTS_DEV" != "off" ]]; then
      P_TTS_DEV="off"; P_TTS_WHY="not in --modules"
    fi
    if [[ ",$P_MODULES," != *",mov,"* && "$P_GES_DEV" != "off" ]]; then
      P_GES_DEV="off"; P_GES_WHY="not in --modules"
    fi
    if [[ ",$P_MODULES," != *",emo,"* && "$P_EMO_DEV" != "off" ]]; then
      P_EMO_DEV="off"; P_EMO_WHY="not in --modules"
    fi
    if [[ ",$P_MODULES," != *",stt,"* && "$P_STT_DEV" == "gpu" ]]; then
      P_STT_DEV="cpu"; P_STT_VRAM=0; P_STT_WHY="stt not in --modules"
    fi
  fi

  # --- Ray resources ---------------------------------------------------------
  if [[ "$GPU_VENDOR" != "none" ]]; then
    [[ "$P_TTS_DEV" == "gpu" ]] && P_TTS_FRAC="$(frac "$P_TTS_VRAM")"
    [[ "$P_GES_DEV" == "gpu" ]] && P_GES_FRAC="$(frac "$P_GES_VRAM")"
    [[ "$P_STT_DEV" == "gpu" ]] && P_STT_FRAC="$(frac "$P_STT_VRAM")"
    P_RAY_GPUS=1
  fi

  # --- RAM / disk requirement ------------------------------------------------
  P_RAM_NEED=$RAM_BASE
  [[ "$P_STT_DEV" == "cpu" ]] && P_RAM_NEED=$(( P_RAM_NEED + RAM_STT[$STT_SIZE] ))
  if [[ "$P_TTS_DEV" == "cpu" ]]; then
    if [[ "$TTS_ENGINE" == "piper" ]]; then
      P_RAM_NEED=$(( P_RAM_NEED + RAM_TTS_PIPER ))
    else
      P_RAM_NEED=$(( P_RAM_NEED + RAM_TTS_CPU ))
    fi
  fi
  [[ "$P_GES_DEV" == "cpu" ]] && P_RAM_NEED=$(( P_RAM_NEED + RAM_GESTURE_CPU ))
  [[ "$P_EMO_DEV" == "cpu" ]] && P_RAM_NEED=$(( P_RAM_NEED + RAM_EMO_PER_SESSION ))
  [[ "$P_LLM_DEV" == "cpu" ]] && P_RAM_NEED=$(( P_RAM_NEED + P_LLM_RAM ))

  P_DISK_NEED=$(( DISK_VENV_BASE + DISK_STT[$STT_SIZE] ))
  case "$GPU_VENDOR" in
    nvidia) P_DISK_NEED=$(( P_DISK_NEED + DISK_TORCH_CUDA )) ;;
    amd)    P_DISK_NEED=$(( P_DISK_NEED + DISK_TORCH_ROCM )) ;;
    *)      P_DISK_NEED=$(( P_DISK_NEED + DISK_TORCH_CPU )) ;;
  esac
  if [[ "$P_TTS_DEV" != "off" ]]; then
    if [[ "$TTS_ENGINE" == "piper" ]]; then
      P_DISK_NEED=$(( P_DISK_NEED + DISK_PIPER ))
    else
      P_DISK_NEED=$(( P_DISK_NEED + DISK_COSYVOICE ))
    fi
  fi
  [[ "$P_GES_DEV" != "off" ]] && P_DISK_NEED=$(( P_DISK_NEED + DISK_EMAGE ))
  [[ "$P_EMO_DEV" != "off" ]] && P_DISK_NEED=$(( P_DISK_NEED + DISK_EMOTION ))
  if (( ! SKIP_SERVICES )); then
    if (( ! QDRANT_REMOTE )); then P_DISK_NEED=$(( P_DISK_NEED + 200 )); fi
    if (( NEED_OLLAMA ));    then P_DISK_NEED=$(( P_DISK_NEED + DISK_SERVICES )); fi
  fi

  # --- Verdict ---------------------------------------------------------------
  if [[ "$P_LLM_DEV" == "off" ]]; then
    P_VERDICT="blocked"
  elif [[ "$P_TTS_DEV" != "off" && "$P_GES_DEV" != "off" ]]; then
    P_VERDICT="full"
  elif [[ "$P_TTS_DEV" != "off" ]]; then
    P_VERDICT="voice"
  else
    P_VERDICT="text"
  fi
}

# row <component> <backend> <device> <budget> <why>. The device colour is
# applied around a pre-padded field so escape codes never shift the columns.
row() {
  local color
  case "$3" in
    gpu)    color="$C_G" ;;
    cpu)    color="$C_Y" ;;
    off)    color="$C_R" ;;
    remote) color="$C_C" ;;
    *)      color="" ;;
  esac
  printf '    %-9s %-26s %s%-7s%s %-12s %s\n' "$1" "$2" "$color" "$3" "$C_RST" "$4" "$5"
}

report_plan() {
  # Budget shown per component: VRAM when it lands on the GPU, otherwise the
  # RAM it will occupy on the host (— when it is not loaded at all).
  local stt_budget llm_budget tts_budget ges_budget emo_budget
  if [[ "$P_STT_DEV" == "gpu" ]]; then stt_budget="$(mb_to_gb "$P_STT_VRAM") GiB vram"
  else stt_budget="$(mb_to_gb "${RAM_STT[$STT_SIZE]}") GiB ram"; fi
  case "$P_LLM_DEV" in
    gpu) llm_budget="$(mb_to_gb "$P_LLM_VRAM") GiB vram" ;;
    cpu) llm_budget="$(mb_to_gb "$P_LLM_RAM") GiB ram" ;;
    *)   llm_budget="-" ;;
  esac
  local llm_backend="ollama $LLM_MODEL"
  [[ "$P_LLM_DEV" == "remote" ]] && llm_backend="$LLM_PROVIDER $LLM_MODEL"
  local embed_backend="ollama $EMBED_MODEL"
  [[ "$P_EMBED_DEV" == "remote" ]] && embed_backend="$EMBED_MODEL"
  local mem_backend="qdrant $QDRANT_VERSION" mem_dev="cpu" mem_why
  if (( QDRANT_REMOTE )); then
    mem_backend="qdrant (remote)"; mem_dev="remote"; mem_why="$QDRANT_URL"
  elif [[ -n "$HAS_DOCKER" ]]; then
    mem_why="via $HAS_DOCKER"
  else
    mem_why="standalone binary"
  fi
  local tts_backend="CosyVoice3-0.5B"
  [[ "$TTS_ENGINE" == "piper" ]] && tts_backend="piper $PIPER_VOICE_NAME"
  case "$P_TTS_DEV" in
    gpu) tts_budget="$(mb_to_gb "$P_TTS_VRAM") GiB vram" ;;
    cpu) if [[ "$TTS_ENGINE" == "piper" ]]; then
           tts_budget="$(mb_to_gb "$RAM_TTS_PIPER") GiB ram"
         else
           tts_budget="$(mb_to_gb "$RAM_TTS_CPU") GiB ram"
         fi ;;
    *)   tts_budget="-" ;;
  esac
  case "$P_GES_DEV" in
    gpu) ges_budget="$(mb_to_gb "$P_GES_VRAM") GiB vram" ;;
    cpu) ges_budget="$(mb_to_gb "$RAM_GESTURE_CPU") GiB ram" ;;
    *)   ges_budget="-" ;;
  esac
  [[ "$P_EMO_DEV" == "cpu" ]] && emo_budget="$(mb_to_gb "$RAM_EMO_PER_SESSION") GiB ram" || emo_budget="-"

  step "Capability plan"
  printf '    %-9s %-26s %-7s %-12s %s\n' "MODULE" "BACKEND" "DEVICE" "BUDGET" "WHY"
  printf '    %s\n' "$(printf '─%.0s' {1..96})"
  row "stt"     "faster-whisper $STT_SIZE" "$P_STT_DEV" "$stt_budget" "$P_STT_WHY"
  row "rag/llm" "$llm_backend"             "$P_LLM_DEV" "$llm_budget" "$P_LLM_WHY"
  row "tts"     "$tts_backend"          "$P_TTS_DEV" "$tts_budget" "$P_TTS_WHY"
  row "mov"     "EMAGE audio"              "$P_GES_DEV" "$ges_budget" "$P_GES_WHY"
  row "emo"     "hubert-large-superb-er"   "$P_EMO_DEV" "$emo_budget" "$P_EMO_WHY"
  row "embed"   "$embed_backend"           "$P_EMBED_DEV" \
      "$([[ "$P_EMBED_DEV" == "gpu" ]] && echo "$(mb_to_gb $VRAM_EMBED) GiB vram" || echo "-")" \
      "$( (( EMBED_REMOTE )) && echo "$EMBED_URL_EFF" || echo "OpenAI-compatible /v1/embeddings" )"
  row "memory"  "$mem_backend"             "$mem_dev"   "-" "$mem_why"

  echo
  printf '    %-22s %s\n' "modules registered" "$P_MODULES"
  printf '    %-22s %s GiB needed / %s GiB present\n' "ram" "$(mb_to_gb $P_RAM_NEED)" "$(mb_to_gb $RAM_TOTAL_MB)"
  printf '    %-22s %s GiB needed / %s GiB free\n' "disk" "$(mb_to_gb $P_DISK_NEED)" "$(mb_to_gb $DISK_FREE_MB)"
  if [[ "$GPU_VENDOR" != "none" ]]; then
    printf '    %-22s ray --num-gpus=%s · fractions tts=%s gesture=%s stt=%s\n' \
      "gpu scheduling" "$P_RAY_GPUS" "$P_TTS_FRAC" "$P_GES_FRAC" "$P_STT_FRAC"
  fi

  echo
  case "$P_VERDICT" in
    full)    printf '    %sVERDICT: full pipeline%s — voice in, voice + gesture out.\n' "$C_G$C_B" "$C_RST" ;;
    voice)   printf '    %sVERDICT: voice pipeline%s — voice in, voice out, no gesture.\n' "$C_G$C_B" "$C_RST" ;;
    text)    printf '    %sVERDICT: text pipeline%s — speech or text in, text out (no local TTS).\n' "$C_Y$C_B" "$C_RST" ;;
    blocked) printf '    %sVERDICT: cannot run here%s — not enough RAM for even the smallest LLM.\n' "$C_R$C_B" "$C_RST" ;;
  esac
}

check_hard_limits() {
  local fatal=0
  if (( RAM_TOTAL_MB < P_RAM_NEED )); then
    err "plan needs $(mb_to_gb $P_RAM_NEED) GiB RAM, machine has $(mb_to_gb $RAM_TOTAL_MB) GiB"
    fatal=1
  fi
  if (( DISK_FREE_MB < P_DISK_NEED )); then
    err "plan needs $(mb_to_gb $P_DISK_NEED) GiB free disk, only $(mb_to_gb $DISK_FREE_MB) GiB available"
    fatal=1
  fi
  [[ "$P_VERDICT" == "blocked" ]] && fatal=1
  if (( fatal )); then
    echo
    note "Options: drop --force-tts/--force-gesture (they reserve CPU RAM),"
    note "--stt-model base, --llm-model qwen2.5:1.5b, --skip-services, or free disk space."
    return 1
  fi
  return 0
}

save_plan() {
  write_file "$STATE_DIR/plan.env" <<EOF
# Generated by scripts/install_local.sh on $(date -Is). Machine-specific.
HURI_PROFILE=$GPU_VENDOR
HURI_GPU_NAME="${GPU_NAME}"
HURI_GPU_VRAM_MB=$GPU_VRAM_MB
HURI_RAY_GPUS=$P_RAY_GPUS
HURI_RAY_CPUS=$CPU_CORES
HURI_VERDICT=$P_VERDICT
HURI_MODULES=$P_MODULES
HURI_STT_SIZE=$STT_SIZE
HURI_STT_DEVICE=$P_STT_DEV
HURI_TTS_DEVICE=$P_TTS_DEV
HURI_GESTURE_DEVICE=$P_GES_DEV
HURI_EMO_DEVICE=$P_EMO_DEV
HURI_LLM_DEVICE=$P_LLM_DEV
HURI_LLM_MODEL=$LLM_MODEL
HURI_LLM_PROVIDER=$LLM_PROVIDER
HURI_LLM_URL=$LLM_URL
HURI_EMBED_MODEL=$EMBED_MODEL
HURI_EMBED_URL=$EMBED_URL_EFF
HURI_EMBED_DEVICE=$P_EMBED_DEV
HURI_QDRANT_URL=$QDRANT_URL
HURI_VERIFY_SSL=$( ((VERIFY_SSL)) && echo true || echo false)
HURI_NEEDS_OLLAMA=$NEED_OLLAMA
HURI_NEEDS_QDRANT=$( ((QDRANT_REMOTE)) && echo 0 || echo 1)
HURI_TTS_FRAC=$P_TTS_FRAC
HURI_GESTURE_FRAC=$P_GES_FRAC
HURI_STT_FRAC=$P_STT_FRAC
HURI_VENV=$VENV_DIR
HURI_PYTHON=$PY_BIN
EOF
}

# =============================================================================
# 3. Stage: system packages
# =============================================================================

SYS_PKGS=()

# compute_system_packages — fills the global SYS_PKGS array for $PKG_MGR.
# Called during detection (to display the plan) and again before installing.
#
# webrtcvad compiles from source (needs a toolchain + Python headers),
# sounddevice dlopens libportaudio, soundfile needs libsndfile, and
# librosa/openai-whisper shell out to ffmpeg.
compute_system_packages() {
  SYS_PKGS=()
  case "$PKG_MGR" in
    apt)    SYS_PKGS=(build-essential git curl ca-certificates pkg-config unzip
                  ffmpeg libsndfile1 libportaudio2 python3-dev) ;;
    dnf)    SYS_PKGS=(gcc gcc-c++ make git curl unzip ffmpeg-free libsndfile portaudio python3-devel) ;;
    pacman) SYS_PKGS=(base-devel git curl unzip ffmpeg libsndfile portaudio) ;;
    zypper) SYS_PKGS=(gcc gcc-c++ make git curl unzip ffmpeg libsndfile1 portaudio python3-devel) ;;
    *)      return 0 ;;
  esac

  # Ubuntu/Debian: creating a venv from the *system* python also needs the
  # matching python3.X-venv package, and the sdist builds (webrtcvad, pyworld)
  # need the headers for *that* interpreter — generic python3-dev is the wrong
  # version when it is not the distro default (deadsnakes on 22.04). Only for an
  # apt-owned interpreter (base prefix /usr): pyenv/uv/conda builds ship their
  # own headers and venv, and their version often has no apt package at all
  # (Debian 13 only packages 3.13, so python3.12-venv does not exist there).
  if [[ "$PKG_MGR" == "apt" && -n "$PY_VERSION" ]] \
     && [[ "$("$PY_BIN" -c 'import sys;print(sys.base_prefix)' 2>/dev/null)" == "/usr" ]]; then
    local pyxy; pyxy="python$(cut -d. -f1,2 <<<"$PY_VERSION")"
    SYS_PKGS+=("$pyxy-venv" "$pyxy-dev")
  fi

  # ROCm userspace for the AMD profile. Without it the repo.radeon.com torch
  # wheels and the ROCm CTranslate2 build cannot even be imported.
  if [[ "$GPU_VENDOR" == "amd" ]]; then
    case "$PKG_MGR" in
      pacman) SYS_PKGS+=("${ROCM_PKGS_PACMAN[@]}") ;;
      apt)    SYS_PKGS+=("${ROCM_PKGS_APT[@]}") ;;
      *)      : ;;   # dnf/zypper: ROCm naming differs too much to guess
    esac
  fi
}

# Ubuntu/Debian only: the ROCm packages live in AMD's own repository, not in
# main/universe. Mirrors deploy/Dockerfile.amd:11-15, but derives the codename
# instead of hardcoding jammy and creates the keyring dir (absent on 22.04).
add_rocm_apt_repo() {
  local codename
  codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")"
  if [[ -z "$codename" ]]; then
    warn "cannot determine the apt codename — add the ROCm repo manually"
    return 0
  fi
  if [[ -r /etc/apt/sources.list.d/rocm.list ]]; then
    ok "ROCm apt repo already configured"
    return 0
  fi
  local sudo_cmd=""; [[ $EUID -ne 0 ]] && sudo_cmd="sudo"
  info "  adding the ROCm $ROCM_VERSION apt repository ($codename)"
  run ${sudo_cmd:+$sudo_cmd} mkdir -p --mode=0755 /etc/apt/keyrings
  run bash -c "curl -fsSL --retry 3 https://repo.radeon.com/rocm/rocm.gpg.key \
    | gpg --dearmor | ${sudo_cmd:+$sudo_cmd }tee /etc/apt/keyrings/rocm.gpg >/dev/null"
  run bash -c "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] \
https://repo.radeon.com/rocm/apt/$ROCM_VERSION $codename main' \
    | ${sudo_cmd:+$sudo_cmd }tee /etc/apt/sources.list.d/rocm.list >/dev/null"
}

install_system_packages() {
  stage_enabled system || return 0
  (( SKIP_SYSTEM )) && { note "system packages skipped (--skip-system)"; return 0; }
  step "System packages"

  compute_system_packages
  local pkgs=("${SYS_PKGS[@]}")
  if [[ -z "$PKG_MGR" ]]; then
    warn "unknown package manager — install a C toolchain, ffmpeg, libsndfile and portaudio yourself"
    return 0
  fi

  # These packages are not optional — webrtcvad and the ROCm runtime are hard
  # requirements — so a missing sudo has to stop the install, not warn and carry
  # on until pip fails with something unrelated.
  local sudo_cmd=""
  [[ $EUID -ne 0 ]] && sudo_cmd="sudo"
  if [[ -n "$sudo_cmd" ]]; then
    if ! have sudo; then
      die "no sudo and not root. Install these yourself, then re-run with --skip-system: ${pkgs[*]}"
    elif ! sudo -n true 2>/dev/null; then
      note "sudo will ask for your password"
    fi
  fi

  [[ "$PKG_MGR" == "apt" && "$GPU_VENDOR" == "amd" ]] && add_rocm_apt_repo

  info "  installing: ${pkgs[*]}"
  case "$PKG_MGR" in
    apt)    run ${sudo_cmd:+$sudo_cmd} apt-get update -qq
            run ${sudo_cmd:+$sudo_cmd} env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}" ;;
    dnf)    run ${sudo_cmd:+$sudo_cmd} dnf install -y "${pkgs[@]}" ;;
            # -Sy, not -S: without a synced database --needed can resolve against
            # a stale index and fail on a package that does exist.
    pacman) run ${sudo_cmd:+$sudo_cmd} pacman -Sy --needed --noconfirm "${pkgs[@]}" ;;
    zypper) run ${sudo_cmd:+$sudo_cmd} zypper install -y "${pkgs[@]}" ;;
  esac
  ok "system packages ready"

  # ROCm talks to the kernel through /dev/kfd and /dev/dri/renderD*. On Ubuntu
  # those are 0660 root:render, so group membership is a hard requirement there;
  # on Arch they are usually 0666 and this is a no-op.
  if [[ "$GPU_VENDOR" == "amd" ]]; then
    if [[ ! -r /dev/kfd || ! -w /dev/kfd ]]; then
      warn "no read/write access to /dev/kfd — ROCm cannot use the GPU"
      note "sudo usermod -aG render,video $USER   # then log out and back in"
    elif ! id -nG 2>/dev/null | grep -qw render; then
      note "you are not in the 'render' group; /dev/kfd is world-accessible here so"
      note "it works, but add yourself if GPU access ever starts failing:"
      note "sudo usermod -aG render,video $USER"
    fi
  fi
}

# =============================================================================
# 4. Stage: Python environment
# =============================================================================

PIP="$VENV_DIR/bin/pip"; VPY="$VENV_DIR/bin/python"

pin_of() { # pin_of <package> <requirements file> — reuse the repo's pinned version
  local pkg="$1" file="$2" line
  line="$(grep -iE "^[[:space:]]*${pkg}[=<>~]" "$REPO_ROOT/$file" 2>/dev/null | head -1 | sed 's/[[:space:]]*#.*//')"
  printf '%s\n' "${line:-$pkg}" | tr -d ' '
}

create_venv() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    local existing
    existing="$("$VENV_DIR/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")"
    case "$existing" in
      3.10|3.11|3.12)
        # A `uv venv` without --seed produces a venv with no pip at all, and the
        # first `$PIP install` then fails with a bare "no such file".
        [[ -x "$VENV_DIR/bin/pip" ]] \
          || die "$VENV_DIR has no pip — run '$VENV_DIR/bin/python -m ensurepip --upgrade', or delete the venv and re-run"
        ok "virtualenv exists: $VENV_DIR (python $existing)"
        return 0 ;;
      *) die "$VENV_DIR runs python $existing (need 3.10–3.12) — delete it, or pass --venv <other path>" ;;
    esac
  fi
  info "  creating virtualenv at $VENV_DIR (python $PY_VERSION)"
  run "$PY_BIN" -m venv "$VENV_DIR"
}

install_python_deps() {
  stage_enabled python || return 0
  (( SKIP_PYTHON )) && { note "python env skipped (--skip-python)"; return 0; }
  step "Python environment"

  create_venv
  (( DRY_RUN )) && { VPY="python"; PIP="pip"; }

  # constraints.txt must be on EVERY pip call (its own header says so): the
  # setuptools<82 cap is what keeps `import webrtcvad` working, and protobuf<5
  # is what keeps Serve replicas from dying on FieldDescriptor.label. This
  # upgrade used to run before C was defined and without it, pulling the newest
  # setuptools and relying on a later resolution to walk it back.
  local C=(-c "$REPO_ROOT/constraints.txt")
  run "$PIP" install "${C[@]}" --upgrade pip setuptools wheel

  # ABI tag of the interpreter that will actually receive the wheels. The ROCm
  # URLs and the CTranslate2 zip member used to hardcode cp312 while
  # detect_python/create_venv accept 3.10-3.12 — on a 3.10/3.11 venv pip then
  # rejected the wheel as "not supported on this platform". repo.radeon.com
  # publishes cp310-cp313 and the CTranslate2 zip carries cp39-cp314, so
  # deriving the tag is strictly better than narrowing the supported range.
  local PYTAG
  if (( DRY_RUN )); then
    # VPY is the bare "python" under --dry-run, which is the *system*
    # interpreter (3.14 on Arch today) and would print a misleading tag.
    PYTAG="cp${PY_VERSION%.*}"
  else
    PYTAG="$("$VPY" -c 'import sys;print("cp%d%d"%sys.version_info[:2])' 2>/dev/null || echo "cp${PY_VERSION%.*}")"
  fi
  PYTAG="${PYTAG//./}"

  # An AMD plan with no ROCm userspace cannot work, and installing the ROCm
  # CTranslate2 wheel would also destroy the CPU fallback (HIP is DT_NEEDED on
  # _ext, not dlopened). Decide that here, before downloading ~7 GB of wheels.
  if [[ "$GPU_VENDOR" == "amd" ]] && ! rocm_have_runtime; then
    warn "no ROCm $ROCM_VERSION runtime found (libamdhip64.so.7 is not on the loader path)"
    note "falling back to a CPU install: CPU torch, CPU STT."
    note "For GPU support install the runtime and re-run:"
    rocm_runtime_hint
    GPU_VENDOR="none"
    P_STT_DEV="cpu"; P_STT_FRAC=0; P_STT_WHY="ROCm userspace missing; int8 on CPU"
    P_RAY_GPUS=0
    [[ "$P_EMO_DEV" != "off" ]] && P_EMO_DEV="cpu"
  fi

  # 1. Vendor torch FIRST, so every later resolution (sentence-transformers,
  #    transformers, faster-whisper) sees a satisfying torch and does not pull
  #    the default PyPI CPU wheel just to have it replaced.
  case "$GPU_VENDOR" in
    nvidia)
      info "  torch $TORCH_CUDA_VERSION (cu121)"
      run "$PIP" install "${C[@]}" --index-url "$TORCH_CUDA_INDEX" \
        "torch==$TORCH_CUDA_VERSION" "torchaudio==$TORCH_CUDA_VERSION"
      ;;
    amd)
      # download.pytorch.org, NOT repo.radeon.com. AMD's ".lw." (lightweight)
      # wheels are built for data centre parts and omit consumer RDNA3: on a
      # gfx1102 (RX 7600/7700S) torch.cuda.is_available() returns True and the
      # device name resolves, then EVERY kernel launch aborts inside HIP with
      #   hip_code_object.cpp:400: Assertion `err == hipSuccess' failed
      # which reads like a hardware fault. Verified arch lists, same torch 2.8.0:
      #   repo.radeon.com   : gfx908 gfx90a gfx942 gfx1030 gfx1100 gfx1101 …  (no gfx1102)
      #   download.pytorch.org: gfx900 … gfx1030 gfx1100 gfx1101 gfx1102 gfx1200 gfx1201
      # The official wheels are also self-contained (they bundle their ROCm libs),
      # so they do not depend on the system ROCm version matching exactly.
      info "  torch $TORCH_ROCM_VERSION ($TORCH_ROCM_FLAVOUR) from download.pytorch.org"
      run "$PIP" install "${C[@]}" --index-url "$TORCH_ROCM_INDEX" \
        "torch==${TORCH_ROCM_VERSION}+${TORCH_ROCM_FLAVOUR}" \
        "torchaudio==${TORCH_ROCM_VERSION}+${TORCH_ROCM_FLAVOUR}"
      # Before requirements.txt, so faster-whisper sees a satisfying ctranslate2
      # and pip does not download the CUDA build from PyPI just to have it
      # replaced a moment later.
      install_ct2_rocm "$PYTAG"
      ;;
    none)
      info "  torch $TORCH_CUDA_VERSION (cpu)"
      run "$PIP" install "${C[@]}" --index-url "$TORCH_CPU_INDEX" \
        "torch==$TORCH_CUDA_VERSION" "torchaudio==$TORCH_CUDA_VERSION"
      ;;
  esac

  # 2. Server + client base (ray[serve], faster-whisper, qdrant, sounddevice…).
  #    serve_requirements.txt first: it is what deploy/Dockerfile.base installs,
  #    and it carries caps (click<8.2, httpx, qdrant-client) that requirements.txt
  #    does not. Installing only requirements.txt made bare metal diverge from the
  #    images.
  info "  serve base requirements"
  run "$PIP" install "${C[@]}" -r "$REPO_ROOT/serve_requirements.txt"
  info "  base requirements"
  run "$PIP" install "${C[@]}" -r "$REPO_ROOT/requirements.txt"

  # 3. (The ROCm CTranslate2 wheel is installed in step 1, alongside torch; the
  #    AMD requirements file is installed last, in step 5 — see the note there.)

  # 4. TTS/gesture extras. On NVIDIA use the pinned file verbatim (same set as
  #    the image); elsewhere derive a CPU-safe variant from it so the pins stay
  #    in one place.
  if [[ "$P_TTS_DEV" != "off" || "$P_GES_DEV" != "off" || "$P_EMO_DEV" != "off" ]]; then
    if [[ "$GPU_VENDOR" == "nvidia" && "$P_TTS_DEV" != "off" ]]; then
      info "  CosyVoice/EMAGE stack (requirements-nvidia.txt)"
      run "$PIP" install "${C[@]}" --extra-index-url "$TORCH_CUDA_INDEX" \
        --extra-index-url https://pypi.ngc.nvidia.com -r "$REPO_ROOT/requirements-nvidia.txt"
    else
      generate_cpu_requirements
      info "  inference extras (.huri-local/requirements-local.generated.txt)"
      run "$PIP" install "${C[@]}" -r "$STATE_DIR/requirements-local.generated.txt"
    fi
  fi

  # 5. AMD extras LAST, exactly like deploy/Dockerfile.amd:79-81. This ordering
  #    matters: requirements-amd.txt pins transformers for the ROCm torch 2.8
  #    wheel, and the step-4 file inherits a different pin from
  #    requirements-nvidia.txt. Installed earlier, the AMD pin was silently
  #    overwritten, and *which* version you ended up with depended on whether
  #    emo/tts/gesture happened to be enabled.
  if [[ "$GPU_VENDOR" == "amd" ]]; then
    info "  AMD/ROCm extras (requirements-amd.txt)"
    run "$PIP" install "${C[@]}" -r "$REPO_ROOT/requirements-amd.txt"
  fi

  # 6. TTS engine.
  if [[ "$P_TTS_DEV" != "off" ]]; then
    if [[ "$TTS_ENGINE" == "piper" ]]; then
      # abi3 wheel (py3.9+, x86_64 and aarch64), and its only runtime deps are
      # onnxruntime + pathvalidate — no torch, so nothing here can disturb the
      # vendor torch installed above.
      info "  piper-tts (onnx, no torch)"
      run "$PIP" install "${C[@]}" piper-tts
    else
      # CosyVoice has no setup.py upstream → clone + PYTHONPATH.
      install_cosyvoice
    fi
  fi

  ok "python environment ready"
}

generate_cpu_requirements() {
  # Derived from requirements-nvidia.txt: drop the torch pins (installed above
  # from the CPU/ROCm index), swap onnxruntime-gpu for the CPU build, and drop
  # the EMAGE *rendering/training* extras — src/modules/gesture/emage only needs
  # torch + transformers + omegaconf + huggingface_hub.
  local out="$STATE_DIR/requirements-local.generated.txt" p
  # Always safe to drop: torch/torchaudio come from the vendor index above, and
  # onnxruntime-gpu is swapped for the CPU build below.
  local -a drop_pkgs=(torch torchaudio onnxruntime-gpu)
  # EMAGE render/training extras — only needed with gesture generation on.
  local -a emage_only=(smplx pyrender trimesh imageio)
  # These look like EMAGE extras but CosyVoice needs them at *inference* time:
  #   lightning, gdown, wget  — imported transitively through Matcha-TTS
  #                             (matcha/models/baselightningmodule.py,
  #                              matcha/utils/utils.py)
  #   pyworld                 — cosyvoice3.yaml names classes in
  #                             cosyvoice.dataset.processor, and HyperPyYAML
  #                             resolves them with pydoc.locate when the model
  #                             loads, so the "training only" module is imported
  #                             regardless. pyworld has no cp312 wheel and builds
  #                             from sdist (needs the C toolchain SYS_PKGS
  #                             already installs).
  local -a cosyvoice_needs=(lightning gdown wget pyworld)
  if [[ "$P_GES_DEV" == "off" ]]; then
    drop_pkgs+=("${emage_only[@]}")
  fi
  if [[ "$P_TTS_DEV" == "off" ]]; then
    drop_pkgs+=("${cosyvoice_needs[@]}")
  fi
  # On ROCm, requirements-amd.txt owns the transformers pin and is installed
  # last (step 5). Leaving it in here too made the two files fight, and the
  # winner depended on which modules were enabled.
  [[ "$GPU_VENDOR" == "amd" ]] && drop_pkgs+=(transformers)
  local drop="^($(IFS='|'; echo "${drop_pkgs[*]}"))([=<>~]|\$)"
  {
    echo "# Generated by scripts/install_local.sh from requirements-nvidia.txt."
    echo "# Vendor: ${GPU_VENDOR}. Do not edit — re-run the installer instead."
    if [[ "$P_TTS_DEV" != "off" ]]; then
      grep -vE "$drop" "$REPO_ROOT/requirements-nvidia.txt" \
        | sed 's/[[:space:]]*#.*//' | grep -vE '^[[:space:]]*$'
      echo "onnxruntime==1.18.0"
    else
      # No CosyVoice: gesture + emotion only.
      for p in transformers librosa soundfile omegaconf huggingface_hub numpy; do
        # Skip anything the vendor file already pins — it is installed last.
        if [[ "$GPU_VENDOR" == "amd" ]] \
           && grep -qiE "^[[:space:]]*${p}[=<>~]" "$REPO_ROOT/requirements-amd.txt" 2>/dev/null; then
          continue
        fi
        pin_of "$p" requirements-nvidia.txt
      done
    fi
  } | write_file "$out"
}

# install_ct2_rocm <python abi tag> — replace the PyPI (CUDA) CTranslate2 with
# the ROCm build. Only safe once the HIP runtime exists: the ROCm wheel links
# libamdhip64/libhipblas/libhiprand as DT_NEEDED on _ext rather than dlopening
# them, so without the runtime even `device="cpu"` stops working — the failure
# is at import, before any device string is read.
install_ct2_rocm() {
  local pytag="${1:-cp312}"
  if ! rocm_have_runtime; then
    warn "skipping the ROCm CTranslate2 wheel — no HIP runtime, it would break CPU STT too"
    return 0
  fi
  info "  CTranslate2 (ROCm wheel, $pytag)"
  local tmp="$STATE_DIR/ct2"
  run mkdir -p "$tmp"
  # 284 MB: keep it between runs instead of re-downloading on every --only python.
  if [[ -s "$tmp/ct2-rocm.zip" ]]; then
    note "reusing $tmp/ct2-rocm.zip"
  else
    run curl -fsSL --retry 3 --retry-delay 2 --retry-connrefused \
      "$CT2_ROCM_URL" -o "$tmp/ct2-rocm.zip"
  fi
  run unzip -o -j "$tmp/ct2-rocm.zip" "temp-linux/ctranslate2-4.7.1-${pytag}-*manylinux*x86_64.whl" -d "$tmp"
  if (( ! DRY_RUN )); then
    local whl; whl="$(find "$tmp" -name "ctranslate2-4.7.1-${pytag}-*.whl" | head -1)"
    [[ -n "$whl" ]] || die "no $pytag CTranslate2 ROCm wheel in $CT2_ROCM_URL"
    # --force-reinstall --no-deps is load-bearing: the ROCm wheel carries the
    # SAME version string (4.7.1) as the PyPI CPU/CUDA build, so a plain
    # `pip install <wheel>` reports "Requirement already satisfied" and silently
    # keeps whichever build is there. That is how STT ended up running the CPU
    # wheel while the log said "CTranslate2 (ROCm wheel)". --no-deps because its
    # dependencies are already pinned by the files installed above.
    run "$PIP" install "${C[@]}" --force-reinstall --no-deps "$whl"
    # Fail here, with the loader error in hand, rather than at the first
    # utterance inside a Serve replica.
    local probe
    if ! probe="$("$VPY" -c 'import ctranslate2; print("ctranslate2", ctranslate2.__version__, "gpus:", ctranslate2.get_cuda_device_count())' 2>&1)"; then
      err "the ROCm CTranslate2 wheel does not load:"
      note "$probe"
      rocm_runtime_hint
      die "CTranslate2 (ROCm) is unusable — STT cannot run"
    fi
    ok "$probe"
  fi
}

install_cosyvoice() {
  local dir="$ASSETS_DIR/cosyvoice"
  if [[ -d "$dir/.git" ]]; then
    ok "CosyVoice checkout present ($dir)"
  else
    info "  cloning CosyVoice @ ${COSYVOICE_COMMIT:0:8}"
    run git clone "$COSYVOICE_REPO" "$dir"
  fi
  run git -C "$dir" fetch --depth 1 origin "$COSYVOICE_COMMIT" || true
  run git -C "$dir" checkout "$COSYVOICE_COMMIT"
  run git -C "$dir" submodule update --init --recursive
}

# =============================================================================
# 5. Stage: model weights (mirrors helm/templates/*-model-init-job.yaml)
# =============================================================================

hf_snapshot() { # hf_snapshot <repo id> <local dir>
  run "$VPY" - "$1" "$2" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(sys.argv[1], local_dir=sys.argv[2])
print("downloaded", sys.argv[1], "->", sys.argv[2])
PY
}

download_models() {
  stage_enabled models || return 0
  (( SKIP_MODELS )) && { note "model download skipped (--skip-models)"; return 0; }
  step "Model weights"
  run mkdir -p "$MODELS_DIR"

  # --- STT: faster-whisper (presence marker: model.bin, as in the Helm job) ---
  local whisper_repo="${WHISPER_REPO_PREFIX}-${STT_SIZE}"
  local whisper_dir="$MODELS_DIR/whisper/$whisper_repo"
  # model.bin alone is not enough: an interrupted snapshot leaves it in place
  # while the tokenizer/config are missing, and the install then "succeeds" with
  # an STT model that cannot load.
  if [[ -f "$whisper_dir/model.bin" && -f "$whisper_dir/config.json" \
        && -f "$whisper_dir/tokenizer.json" && -f "$whisper_dir/vocabulary.txt" ]]; then
    ok "whisper: already at $whisper_dir"
  else
    [[ -f "$whisper_dir/model.bin" ]] && warn "whisper snapshot at $whisper_dir is incomplete — re-downloading"
    info "  whisper $STT_SIZE → $whisper_dir"
    hf_snapshot "$whisper_repo" "$whisper_dir"
  fi

  # --- TTS: piper voice (61 MB ONNX + its json sidecar) ---
  if [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "piper" ]]; then
    local piper_dir="$MODELS_DIR/piper"
    local piper_onnx="$piper_dir/$PIPER_VOICE_NAME.onnx"
    if [[ -f "$piper_onnx" && -f "$piper_onnx.json" ]]; then
      ok "piper voice: already at $piper_onnx"
    else
      info "  piper voice $PIPER_VOICE_NAME → $piper_dir"
      run mkdir -p "$piper_dir"
      run "$VPY" -m piper.download_voices --download-dir "$piper_dir" "$PIPER_VOICE_NAME"
    fi
  fi

  # --- TTS: CosyVoice3 from ModelScope (markers: cosyvoice3.yaml + llm.pt) ---
  if [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "cosyvoice" ]]; then
    local cosy_dir="$MODELS_DIR/cosytts/$COSYTTS_MODEL_ID"
    if [[ -f "$cosy_dir/cosyvoice3.yaml" && -f "$cosy_dir/llm.pt" ]]; then
      ok "cosyvoice3: already at $cosy_dir"
    else
      info "  CosyVoice3 → $cosy_dir (several GiB)"
      run "$VPY" - "$COSYTTS_MODEL_ID" "$cosy_dir" <<'PY'
import sys
from modelscope import snapshot_download
snapshot_download(sys.argv[1], local_dir=sys.argv[2])
print("downloaded", sys.argv[1], "->", sys.argv[2])
PY
    fi
  fi

  # --- Gesture: EMAGE ---------------------------------------------------------
  if [[ "$P_GES_DEV" != "off" ]]; then
    local emage_dir="$MODELS_DIR/emage/$EMAGE_REPO_ID"
    if [[ -d "$emage_dir" && -n "$(ls -A "$emage_dir" 2>/dev/null)" ]]; then
      ok "emage: already at $emage_dir"
    else
      info "  EMAGE → $emage_dir"
      hf_snapshot "$EMAGE_REPO_ID" "$emage_dir"
    fi
  fi

  # --- Emotion: prefetched into the HF cache (the module loads it by repo id) --
  if [[ "$P_EMO_DEV" != "off" ]]; then
    info "  prosody model $EMOTION_REPO_ID → HF cache"
    run "$VPY" - "$EMOTION_REPO_ID" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(sys.argv[1])
print("cached", sys.argv[1])
PY
  fi

  # --- Reference voice sample -------------------------------------------------
  if [[ "$P_TTS_DEV" != "off" ]]; then
    if [[ -n "$VOICE_SAMPLE" ]]; then
      [[ -f "$VOICE_SAMPLE" ]] || die "--voice-sample '$VOICE_SAMPLE' not found"
      run cp "$VOICE_SAMPLE" "$ASSETS_DIR/voice.wav"
      ok "voice sample installed at $ASSETS_DIR/voice.wav"
    elif [[ -f "$ASSETS_DIR/voice.wav" ]]; then
      ok "voice sample present at $ASSETS_DIR/voice.wav"
    else
      warn "no $ASSETS_DIR/voice.wav — TTS will start but zero-shot synthesis needs it."
      note "Add one with: scripts/install_local.sh --only models,config \\"
      note "    --voice-sample /path/to/voice.wav --voice-transcript 'exact words spoken'"
      note "(--only config alone will NOT copy the file — 'models' is the stage that does)"
    fi
  fi
  ok "models ready"
}

# =============================================================================
# 6. Stage: services (Qdrant + Ollama)
# =============================================================================

start_qdrant() {
  if curl -fsS --max-time 2 "${QDRANT_URL%/}/readyz" >/dev/null 2>&1; then
    ok "qdrant already listening at $QDRANT_URL"
    note "not managed by HuRI — start.sh will check it but cannot start it"
    return 0
  fi
  local data="$STATE_DIR/qdrant"
  run mkdir -p "$data"
  if [[ -n "$HAS_DOCKER" ]]; then
    info "  starting qdrant via $HAS_DOCKER"
    if (( ! DRY_RUN )) && "$HAS_DOCKER" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx huri-qdrant; then
      run "$HAS_DOCKER" start huri-qdrant
    else
      # Qdrant always listens on 6333/6334 *inside* the container; only the
      # published host ports follow --qdrant-url.
      run "$HAS_DOCKER" run -d --name huri-qdrant --restart unless-stopped \
        -p "$QDRANT_PORT:6333" -p "$QDRANT_GRPC_PORT:6334" \
        -v "$data:/qdrant/storage" "$QDRANT_IMAGE"
    fi
  else
    info "  installing standalone qdrant binary (no container runtime found)"
    # The release tag IS "v1.12.4" — stripping the v gave a 404 on every host
    # without docker, and the error was swallowed into a warning while start.sh
    # went on to look for a binary that was never installed.
    local url="https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/qdrant-x86_64-unknown-linux-gnu.tar.gz"
    run mkdir -p "$STATE_DIR/bin"
    if [[ ! -x "$STATE_DIR/bin/qdrant" ]]; then
      if ! run curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$STATE_DIR/qdrant.tar.gz"; then
        rm -f "$STATE_DIR/qdrant.tar.gz"
        die "qdrant download failed ($url) — install qdrant yourself, or point --qdrant-url at a running instance"
      fi
      # Guard the extract: a 404/HTML body would fail inside tar and surface as
      # an opaque trap message.
      if ! run tar -xzf "$STATE_DIR/qdrant.tar.gz" -C "$STATE_DIR/bin"; then
        rm -f "$STATE_DIR/qdrant.tar.gz"
        die "the qdrant archive is not a valid tarball — check $url"
      fi
      run chmod +x "$STATE_DIR/bin/qdrant"
    fi
    # The binary resolves ./config/config.yaml relative to its working directory
    # (start.sh runs it from .huri-local), unlike the image which ships one.
    write_file "$STATE_DIR/config/config.yaml" <<YAML
storage:
  storage_path: ./qdrant
service:
  host: 127.0.0.1
  http_port: $QDRANT_PORT
  grpc_port: $QDRANT_GRPC_PORT
telemetry_disabled: true
YAML
    note "qdrant will be started by .huri-local/start.sh"
  fi
}

install_ollama() {
  if (( HAS_OLLAMA )); then ok "ollama already installed"; return 0; fi
  info "  ollama is not installed"
  note "Official installer: curl -fsSL https://ollama.com/install.sh | sh"
  if confirm "    Run the official Ollama installer now?"; then
    run bash -c 'curl -fsSL https://ollama.com/install.sh | sh'
    HAS_OLLAMA=1
  else
    warn "skipping ollama — install it yourself, then re-run with --only services"
    return 0
  fi
}

ollama_up() {
  curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1
}

pull_ollama_models() {
  (( HAS_OLLAMA )) || return 0
  if ! ollama_up; then
    info "  starting 'ollama serve' in the background"
    if (( ! DRY_RUN )); then
      nohup ollama serve >"$STATE_DIR/ollama.log" 2>&1 &
      local i
      for i in $(seq 1 30); do ollama_up && break; sleep 1; done
    fi
  fi
  if ! ollama_up && (( ! DRY_RUN )); then
    warn "ollama is not answering on :11434 — pull the models manually:"
    note "ollama pull $LLM_MODEL && ollama pull $EMBED_MODEL"
    return 0
  fi
  if (( ! LLM_REMOTE )); then
    info "  pulling $LLM_MODEL (several GiB)"
    run ollama pull "$LLM_MODEL"
  fi
  if (( ! EMBED_REMOTE )); then
    info "  pulling $EMBED_MODEL"
    run ollama pull "$EMBED_MODEL"
  fi
}

# probe_remote <label> <base url> — best-effort reachability check for an
# endpoint this installer does not manage.
probe_remote() {
  local label="$1" url="$2" path="/v1/models" args=()
  [[ "$LLM_PROVIDER" == "ollama" ]] && path="/api/tags"
  (( VERIFY_SSL )) || args+=(-k)
  [[ -n "$LLM_API_KEY" ]] && args+=(-H "Authorization: Bearer $LLM_API_KEY")
  if curl -fsS --max-time 8 "${args[@]}" "${url%/}$path" >/dev/null 2>&1; then
    ok "$label reachable: ${url%/}$path"
  else
    warn "$label did not answer at ${url%/}$path — check the URL, key or TLS trust"
  fi
}

install_services() {
  stage_enabled services || return 0
  (( SKIP_SERVICES )) && { note "services skipped (--skip-services)"; return 0; }
  step "Services"
  if (( QDRANT_REMOTE )); then
    ok "qdrant: remote ($QDRANT_URL) — nothing to install"
  else
    start_qdrant
  fi
  if (( NEED_OLLAMA )); then
    install_ollama
    pull_ollama_models
  else
    ok "llm + embeddings: remote ($LLM_URL) — no local ollama needed"
  fi
  ok "services ready"
}

# =============================================================================
# 7. Stage: generated configuration + run scripts
# =============================================================================

generate_configs() {
  stage_enabled config || return 0
  step "Configuration"

  # Computed once here and reused by the Serve config, env.sh and start.sh, so
  # every entrypoint agrees about the GPU architecture workaround.
  GFX_OVERRIDE=""
  if [[ "$GPU_VENDOR" == "amd" ]] && (( ! DRY_RUN )); then
    GFX_OVERRIDE="$(rocm_gfx_override "$VPY" || true)"
    if [[ -n "$GFX_OVERRIDE" ]]; then
      warn "this GPU's arch is not in the installed torch build — setting HSA_OVERRIDE_GFX_VERSION=$GFX_OVERRIDE"
      note "without it every GPU kernel launch aborts inside HIP"
    fi
  fi

  local cosy_dir="$ASSETS_DIR/cosyvoice"
  local cosy_model="$MODELS_DIR/cosytts/$COSYTTS_MODEL_ID"
  local whisper_path="$MODELS_DIR/whisper/${WHISPER_REPO_PREFIX}-${STT_SIZE}"
  local emage_path="$MODELS_DIR/emage/$EMAGE_REPO_ID"
  local pythonpath="$REPO_ROOT"
  # CosyVoice needs its checkout on the path; piper is a normal installed package.
  [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "cosyvoice" ]] && pythonpath="$REPO_ROOT:$cosy_dir:$cosy_dir/third_party/Matcha-TTS"
  local stt_workers=1
  if [[ "$P_STT_DEV" == "cpu" ]]; then
    stt_workers=$(( CPU_CORES / 4 ))
    (( stt_workers < 1 )) && stt_workers=1
    (( stt_workers > 4 )) && stt_workers=4
  fi
  local ges_mem_frac="0"
  [[ "$P_GES_DEV" == "gpu" ]] && ges_mem_frac="$(frac_ceil "$P_GES_VRAM")"

  local serve_cfg="$REPO_ROOT/config/huri_local.generated.yaml"
  {
    cat <<EOF
# HuRI — Ray Serve config generated by scripts/install_local.sh
# Host: $(hostname) · $(date -Is)
# Profile: ${GPU_VENDOR}${GPU_NAME:+ (${GPU_NAME}, $(mb_to_gb "$GPU_VRAM_MB") GiB)} · verdict: $P_VERDICT
#
# Regenerate with:  scripts/install_local.sh --only config
# Start with:       .huri-local/start.sh
#
# Device plan:
#   stt      $P_STT_DEV   — $P_STT_WHY
#   rag/llm  $P_LLM_DEV   — $P_LLM_WHY
#   tts      $P_TTS_DEV   — $P_TTS_WHY
#   mov      $P_GES_DEV   — $P_GES_WHY
#   emo      $P_EMO_DEV   — $P_EMO_WHY

proxy_location: EveryNode
http_options:
  host: 0.0.0.0
  port: 8000

applications:
  - name: huri-app
    route_prefix: /
    import_path: src.app:app
    runtime_env:
      env_vars:
        RAY_COLOR_PREFIX: "1"
        PYTHONPATH: "$pythonpath"

        # Only these modules are registered, so no deployment is created for the
        # ones this machine cannot run (see src/modules/modules.py).
        HURI_MODULES: "$P_MODULES"

        # --- STT (faster-whisper) ---
        HURI_STT_MODEL_PATH: "$whisper_path"
        HURI_STT_NUM_WORKERS: "$stt_workers"
        # The plan's device decision, translated into faster-whisper's own
        # vocabulary (cpu | cuda | auto — "gpu" is not a legal value, and
        # CTranslate2 calls the ROCm backend "cuda" as well). Without these the
        # replica fell back to device="auto"/compute_type="auto", so the plan was
        # decorative and CPU STT silently ran float32 instead of int8.
        HURI_STT_DEVICE: "$(stt_ct2_device)"
        HURI_STT_COMPUTE_TYPE: "$(stt_ct2_compute_type)"
EOF
    if [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "piper" ]]; then
      cat <<EOF

        # --- TTS (piper) ---
        # ONNX only: no torch, no GPU, ~30x faster than realtime on CPU.
        # Switch engines with --tts-engine cosyvoice (needs an NVIDIA GPU).
        HURI_TTS_ENGINE: "piper"
        HURI_PIPER_VOICE: "$MODELS_DIR/piper/$PIPER_VOICE_NAME.onnx"
EOF
    fi
    if [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "cosyvoice" ]]; then
      cat <<EOF

        # --- TTS (CosyVoice3) ---
        HURI_TTS_ENGINE: "cosyvoice"
        HURI_MODEL_PATH: "$cosy_model"
        HURI_COSY_DIR: "$cosy_dir"
        HURI_VOICE_SAMPLE_PATH: "$ASSETS_DIR/voice.wav"
        # "<instruction><|endofprompt|><transcript of voice.wav>" — the transcript
        # MUST come after the marker or the LM speaks it out loud.
        HURI_VOICE_TRANSCRIPT: "You are a helpful assistant.<|endofprompt|>$VOICE_TRANSCRIPT"
        HURI_TTS_FP16: "$([[ $P_TTS_DEV == gpu ]] && echo 1 || echo 0)"
EOF
    fi
    if [[ "$P_GES_DEV" != "off" ]]; then
      cat <<EOF

        # --- Gesture (EMAGE) ---
        HURI_EMAGE_REPO: "$emage_path"
        HURI_GESTURE_CONTEXT_SEC: "2.0"
        HURI_GESTURE_MIN_CHUNK_SEC: "0.5"
        # Caps the EMAGE process to this share of the device so TTS keeps the rest.
        HURI_GESTURE_GPU_MEM_FRACTION: "$ges_mem_frac"
EOF
    fi
    if [[ "$GPU_VENDOR" == "nvidia" ]]; then
      cat <<EOF

        NVIDIA_VISIBLE_DEVICES: "all"
        NVIDIA_DRIVER_CAPABILITIES: "compute,utility"
EOF
    fi
    if [[ "$GPU_VENDOR" == "amd" ]] && (( GPU_COUNT > 1 )); then
      cat <<EOF

        # This host has $GPU_COUNT AMD devices. An APU's iGPU has a different gfx
        # target from the dGPU and the CTranslate2 ROCm build carries no kernels
        # for it, so a replica landing there fails with "no kernel image is
        # available for execution on the device". start.sh pins Ray to GPU index
        # $GPU_INDEX via HIP_VISIBLE_DEVICES; override with --gpu-index.
EOF
    fi
    if [[ -n "$GFX_OVERRIDE" ]]; then
      cat <<EOF

        # This GPU's gfx target is absent from the installed torch build, so
        # every kernel launch would abort inside HIP with an assertion that
        # looks like a hardware fault. Report a supported same-family arch so
        # the prebuilt kernels load. See rocm_gfx_override().
        HSA_OVERRIDE_GFX_VERSION: "$GFX_OVERRIDE"
EOF
    fi
    cat <<EOF
        # 10s is too tight for a first-run snapshot on a slow link; a timeout
        # mid-download leaves a partial model that looks present.
        HF_HUB_DOWNLOAD_TIMEOUT: "60"

    deployments:
      # Ingress + per-session router. CPU only.
      - name: HuRI
        ray_actor_options:
          num_cpus: 1
          num_gpus: 0

      - name: STTHandle
        num_replicas: 1
        ray_actor_options:
          num_cpus: 1
          num_gpus: $P_STT_FRAC

      - name: RAGHandle
        num_replicas: 1
        ray_actor_options:
          num_cpus: 1
          num_gpus: 0
        user_config:
          qdrant_url: "$QDRANT_URL"
          llm_provider: "$LLM_PROVIDER"
          llm_url: "$LLM_URL"
          llm_model: "$LLM_MODEL"
          embedding_url: "$EMBED_URL_EFF"
          embedding_model: "$EMBED_MODEL"
          verify_ssl: $( ((VERIFY_SSL)) && echo true || echo false)
          memory_maintenance_check_hours: 6.0
          # llm_api_key is deliberately absent: it comes from HURI_LLM_API_KEY
          # (.huri-local/secrets.env, sourced by start.sh) so no secret is
          # written to disk here.
EOF
    if [[ "$P_TTS_DEV" != "off" ]]; then
      cat <<EOF

      - name: TTSHandle
        ray_actor_options:
          num_cpus: 1
          num_gpus: $P_TTS_FRAC
EOF
    fi
    if [[ "$P_EMO_DEV" != "off" ]]; then
      cat <<EOF

      - name: EMOHandle
        num_replicas: 1
        ray_actor_options:
          num_cpus: 1
          num_gpus: 0
EOF
    fi
    if [[ "$P_GES_DEV" != "off" ]]; then
      cat <<EOF

      - name: MOVHandle
        ray_actor_options:
          num_cpus: 1
          num_gpus: $P_GES_FRAC
EOF
    fi
  } | write_file "$serve_cfg"
  ok "serve config → config/huri_local.generated.yaml"

  # Same runtime_env, as a sourceable .env: `serve deploy` injects those vars
  # into the replicas, but `python -m src.launch_huri`, ingestion.py or a pytest
  # run get nothing. env.sh sources this so both paths see identical settings.
  # The YAML is our own output, so the shape below is stable.
  if (( ! DRY_RUN )); then
    while IFS=$'\t' read -r k v; do
      # Double-quoted, not %q: `printf %q` emits shell-escaped forms such as
      # vad\,stt\,tag, which `source` handles but no dotenv parser does — and
      # env.sh advertises this file as a .env. Values here are paths and
      # comma-separated lists, so escaping " and \ is sufficient.
      v="${v//\\/\\\\}"; v="${v//\"/\\\"}"
      printf '%s="%s"\n' "$k" "$v"
    done < <(awk '
      /^    runtime_env:/      { inside = 1; next }
      inside && /^    deployments:/ { exit }
      inside && /^        [A-Za-z_][A-Za-z0-9_]*:/ {
        key = $0; sub(/:.*$/, "", key); sub(/^ +/, "", key)
        val = $0; sub(/^[^:]*:[[:space:]]*/, "", val)
        sub(/^"/, "", val); sub(/"$/, "", val)
        printf "%s\t%s\n", key, val
      }' "$serve_cfg") >"$STATE_DIR/huri.env"
    ok "environment → .huri-local/huri.env ($(grep -c . "$STATE_DIR/huri.env") vars)"
  fi

  # --- Matching client config -------------------------------------------------
  local client_cfg="$REPO_ROOT/config/client_local.generated.yaml"
  {
    echo "# Client config generated by scripts/install_local.sh — modules match"
    echo "# HURI_MODULES in config/huri_local.generated.yaml ($P_MODULES)."
    echo "huri_url: ws://localhost:8000/session"
    echo "interface_path: src.interfaces.cli_interface:cli_interface"
    echo
    echo "senders:"
    echo "  audio:"
    echo "    name: audio"
    echo "    topic: audio.in"
    echo "    args:"
    echo "      sample_rate: 16000"
    echo "      frame_duration: 0.030"
    echo "  text:"
    echo "    name: text"
    echo "    topic: question"
    echo "    args:"
    echo "      sample_rate: 16000"
    echo "      frame_duration: 0.030"
    echo
    echo "hooks:"
    echo "  token:"
    echo "    name: token"
    echo "    topics: [token]"
    if [[ "$P_TTS_DEV" != "off" ]]; then
      echo "  audio:"
      echo "    name: audio"
      echo "    topics: [audio.out]"
      echo "    args:"
      echo "      incoming_sample_rate: \${senders.audio.args.sample_rate}"
      echo "      sample_rate: 44100"
      # AudioHook.__init__ declares save_audio_dir with no default, and hooks are
      # built with **hook.args — so omitting it is a hard TypeError at client
      # startup, before the websocket is even opened. Empty = do not save.
      echo "      save_audio_dir: \"\""
    fi
    if [[ "$P_GES_DEV" != "off" ]]; then
      echo "  motion:"
      echo "    name: motion"
      echo "    topics: [motion]"
    fi
    echo
    echo "modules:"
    echo "  vad:"
    echo "    name: vad"
    echo "    args:"
    echo "      vad_agressiveness: 3"
    echo "      silence_duration: 1.5"
    echo "      block_duration: \${senders.audio.args.frame_duration}"
    echo "    logging: INFO"
    echo "  stt:"
    echo "    name: stt"
    echo "    args:"
    echo "      language: en"
    echo "      block_duration: \${senders.audio.args.frame_duration}"
    echo "    logging: INFO"
    echo "  tag:"
    echo "    name: tag"
    echo "    args:"
    echo "      min_prefix: 3"
    echo "    logging: INFO"
    if [[ "$P_EMO_DEV" != "off" ]]; then
      echo "  emo:"
      echo "    name: emo"
      echo "    args:"
      echo "      block_duration: \${senders.audio.args.frame_duration}"
      echo "  eag:"
      echo "    name: eag"
    fi
    echo "  qag:"
    echo "    name: qag"
    echo "    args:"
    # QAG defaults to use_emotion=True and then holds every question until an
    # `emotion` event arrives. With emo/eag pruned from the plan nothing ever
    # publishes one, so the pipeline stalls inside QAG — no error, no answer.
    if [[ "$P_EMO_DEV" != "off" ]]; then
      echo "      use_emotion: true"
    else
      echo "      use_emotion: false"
    fi
    echo "  rag:"
    echo "    name: rag"
    echo "    args:"
    echo "      language: en"
    echo "      tone: formal"
    echo "      response_format: paragraph"
    echo "      max_length: 1024"
    echo "    logging: INFO"
    if [[ "$P_TTS_DEV" != "off" ]]; then
      echo "  tts:"
      echo "    name: tts"
      # No args: TTS.__init__ takes only the deployment handle, and hooks/modules
      # are built with **cfg.args — so any key here is a hard TypeError at
      # session start. The old `min_clause_chars: 20` is a leftover from a design
      # where HuRI buffered clauses itself; CosyVoice's frontend now does the
      # segmentation (see the TTS docstring) and nothing reads that key.
      echo "    logging: INFO"
    fi
    if [[ "$P_GES_DEV" != "off" ]]; then
      echo "  mov:"
      echo "    name: mov"
      echo "    logging: INFO"
    fi
  } | write_file "$client_cfg"
  ok "client config → config/client_local.generated.yaml"

  write_secrets
  generate_run_scripts
  save_plan
}

# The API key never goes into config/*.generated.yaml. It lives here, 0600, and
# start.sh sources it before `ray start` so every Serve replica inherits
# HURI_LLM_API_KEY (rag.py reads it as the llm_api_key default).
write_secrets() {
  local esc="${LLM_API_KEY//\'/\'\\\'\'}"
  write_file "$STATE_DIR/secrets.env" <<EOF
# HuRI local secrets — sourced by .huri-local/start.sh. Not committed.
# Change the key with: scripts/install_local.sh --only config --llm-api-key ...
export HURI_LLM_API_KEY='${esc}'
EOF
  (( DRY_RUN )) || chmod 600 "$STATE_DIR/secrets.env"
  if [[ -n "$LLM_API_KEY" ]]; then
    ok "api key → .huri-local/secrets.env (0600, exported as HURI_LLM_API_KEY)"
  fi
}

generate_run_scripts() {
  # Does HuRI own a Qdrant it is able to start? start_qdrant() returns early
  # when something already answers on the port, in which case no huri-qdrant
  # container and no bundled binary were ever created. Decided here rather than
  # in the services stage so that `--only config` gets it right too.
  local qdrant_owned=0
  if [[ -x "$STATE_DIR/bin/qdrant" ]]; then
    qdrant_owned=1
  elif [[ -n "$HAS_DOCKER" ]] && (( ! DRY_RUN )) \
       && "$HAS_DOCKER" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx huri-qdrant; then
    qdrant_owned=1
  fi

  # Built as a plain string rather than a ${VAR:+...} inside the heredoc below:
  # that construct does not survive the surrounding escaped ${...} expansions.
  local gfx_export=""
  if [[ -n "$GFX_OVERRIDE" ]]; then
    gfx_export="# This GPU's gfx target is missing from the installed torch build; report a
# supported same-family arch or every kernel launch aborts inside HIP.
export HSA_OVERRIDE_GFX_VERSION=$GFX_OVERRIDE"
  fi

  write_file "$STATE_DIR/env.sh" <<EOF
# shellcheck shell=bash
# Source this for a shell configured exactly like a Serve replica:
#     source .huri-local/env.sh
#     python -m src.launch_huri                      # run HuRI without serve deploy
#     python -m src.modules.rag.ingestion --help     # feed the RAG memory
export HURI_ROOT="$REPO_ROOT"
export HURI_STATE="$STATE_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Model paths, module allow-list, TTS/gesture tuning — the same values the
# generated Serve config injects into every replica (regenerate both with
# 'scripts/install_local.sh --only config').
if [ -r "$STATE_DIR/huri.env" ]; then
  set -a; . "$STATE_DIR/huri.env"; set +a
fi
# API key, if any: kept out of the config files on purpose.
if [ -r "$STATE_DIR/secrets.env" ]; then
  # shellcheck disable=SC1091
  . "$STATE_DIR/secrets.env"
fi

case ":\${PYTHONPATH:-}:" in
  *":\$HURI_ROOT:"*) ;;
  *) export PYTHONPATH="\$HURI_ROOT\${PYTHONPATH:+:\$PYTHONPATH}" ;;
esac
$gfx_export
EOF

  {
    cat <<EOF
#!/usr/bin/env bash
# Start the local HuRI stack (generated by scripts/install_local.sh).
set -Eeuo pipefail
cd "$REPO_ROOT"
source "$STATE_DIR/env.sh"

# Secrets are exported *before* ray start so every Serve replica inherits them
# (rag.py reads HURI_LLM_API_KEY). A cluster already running keeps its old env —
# run stop.sh first after changing the key.
# shellcheck disable=SC1091
[ -r "$STATE_DIR/secrets.env" ] && source "$STATE_DIR/secrets.env"
EOF

    if (( QDRANT_REMOTE )); then
      cat <<EOF

# --- 1. Qdrant: remote ($QDRANT_URL), nothing to start ----------------------
EOF
    elif (( qdrant_owned )); then
      cat <<EOF

# --- 1. Qdrant --------------------------------------------------------------
if ! curl -fsS --max-time 2 ${QDRANT_URL%/}/readyz >/dev/null 2>&1; then
  if [ -x "$STATE_DIR/bin/qdrant" ]; then
    echo "[huri] starting qdrant (binary)"
    (cd "$STATE_DIR" && { nohup "$STATE_DIR/bin/qdrant" >"$STATE_DIR/qdrant.log" 2>&1 & echo \$! >"$STATE_DIR/qdrant.pid"; })
  elif command -v ${HAS_DOCKER:-docker} >/dev/null 2>&1; then
    echo "[huri] starting qdrant container"
    ${HAS_DOCKER:-docker} start huri-qdrant >/dev/null 2>&1 || true
  fi
  for _ in \$(seq 1 30); do
    curl -fsS --max-time 1 ${QDRANT_URL%/}/readyz >/dev/null 2>&1 && break
    sleep 1
  done
fi
if ! curl -fsS --max-time 2 ${QDRANT_URL%/}/readyz >/dev/null 2>&1; then
  echo "[huri] WARNING: qdrant is not answering at ${QDRANT_URL%/}."
  echo "[huri]          RAG retrieval and memory will silently return nothing."
fi
EOF
    else
      cat <<EOF

# --- 1. Qdrant: not managed by HuRI -----------------------------------------
# Something was already listening on ${QDRANT_URL%/} when the installer ran, so
# it created neither a huri-qdrant container nor a bundled binary. There is
# nothing to start here — but it is still checked, because RAG fails silently
# rather than loudly when the vector DB is missing.
if ! curl -fsS --max-time 2 ${QDRANT_URL%/}/readyz >/dev/null 2>&1; then
  echo "[huri] WARNING: qdrant is not answering at ${QDRANT_URL%/}, and HuRI does"
  echo "[huri]          not manage this instance. Start it yourself, or free the"
  echo "[huri]          port and re-run: scripts/install_local.sh --only services"
fi
EOF
    fi

    if (( NEED_OLLAMA )); then
      cat <<EOF

# --- 2. Ollama --------------------------------------------------------------
if ! curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  if command -v ollama >/dev/null 2>&1; then
    echo "[huri] starting ollama"
    nohup ollama serve >"$STATE_DIR/ollama.log" 2>&1 &
    echo \$! >"$STATE_DIR/ollama.pid"
    for _ in \$(seq 1 30); do
      curl -fsS --max-time 1 http://localhost:11434/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
  fi
fi
EOF
    else
      cat <<EOF

# --- 2. LLM + embeddings: remote ($LLM_URL), nothing to start ---------------
EOF
    fi

    cat <<EOF

# --- 3. Ray head ------------------------------------------------------------
# Started from the repo root so replicas can import src.app.
EOF

    if [[ "$GPU_VENDOR" == "amd" ]] && (( GPU_COUNT > 1 )); then
      cat <<EOF
# This host exposes $GPU_COUNT AMD devices, so Ray is given only index
# $GPU_INDEX ($GPU_NAME). An APU's iGPU has a different gfx target and the
# CTranslate2 ROCm wheel carries no kernels for it, so an STT replica scheduled
# there fails with "no kernel image is available for execution on the device".
# HIP_VISIBLE_DEVICES, not ROCR_VISIBLE_DEVICES: Ray 2.55 raises on the latter,
# and the two compose rather than alias — setting both selects nothing at all.
# Re-run the installer with --gpu-index N to pick a different device.
export HIP_VISIBLE_DEVICES=$GPU_INDEX
EOF
    fi
    if [[ -n "$GFX_OVERRIDE" ]]; then
      cat <<EOF
# Exported before 'ray start' so the head and every replica inherit it.
export HSA_OVERRIDE_GFX_VERSION=$GFX_OVERRIDE
EOF
    fi

    cat <<EOF
if ! ray status >/dev/null 2>&1; then
  echo "[huri] starting ray head"
  ray start --head --num-cpus=$CPU_CORES --num-gpus=$P_RAY_GPUS \\
    --dashboard-host=127.0.0.1 --disable-usage-stats
fi

# --- 4. HuRI ----------------------------------------------------------------
echo "[huri] deploying config/huri_local.generated.yaml"
serve deploy config/huri_local.generated.yaml

# 'serve deploy' only posts the config to the dashboard agent and returns 0; it
# never waits. Without this poll the script printed "HuRI is deploying" and
# exited 0 even when a replica constructor was already raising ImportError and
# the application was heading for DEPLOY_FAILED.
# Only the application-level 'status:' is read (the first one in the YAML) —
# a per-deployment UNHEALTHY is transient during startup and must not abort us.
echo "[huri] waiting for huri-app to become RUNNING (up to 300s)"
huri_state=""
for _ in \$(seq 1 100); do
  huri_state="\$(serve status --name huri-app 2>/dev/null | awk '/^status:/ {print \$2; exit}')"
  [ "\$huri_state" = "RUNNING" ] && break
  [ "\$huri_state" = "DEPLOY_FAILED" ] && break
  sleep 3
done

if [ "\$huri_state" = "RUNNING" ]; then
  echo
  echo "HuRI is up. Talk to it with:"
  echo "    source .huri-local/env.sh"
  echo "    python -m src.client --config config/client_local.generated.yaml"
  echo
  echo "Status: .huri-local/status.sh   Dashboard: http://127.0.0.1:8265"
else
  echo
  echo "[huri] FAILED: huri-app did not come up (last status: \${huri_state:-unknown})."
  serve status --name huri-app 2>/dev/null | sed -n '1,60p' || true
  echo
  echo "[huri] Replica tracebacks are in /tmp/ray/session_latest/logs/serve/"
  exit 1
fi
EOF
  } | write_file "$STATE_DIR/start.sh"
  run chmod +x "$STATE_DIR/start.sh"

  write_file "$STATE_DIR/stop.sh" <<EOF
#!/usr/bin/env bash
# Stop the local HuRI stack.
set -Euo pipefail
cd "$REPO_ROOT"
source "$STATE_DIR/env.sh"
serve shutdown -y >/dev/null 2>&1 || true
ray stop >/dev/null 2>&1 || true
if [ "\${1:-}" = "--all" ]; then
  # Only stop what start.sh itself started. The previous version ran
  # \`pkill -f "ollama serve"\`, which kills any Ollama on the machine —
  # including one the user runs for unrelated work.
  ${HAS_DOCKER:-docker} stop huri-qdrant >/dev/null 2>&1 || true
  for svc in qdrant ollama; do
    pidfile="$STATE_DIR/\$svc.pid"
    if [ -r "\$pidfile" ]; then
      pid="\$(cat "\$pidfile" 2>/dev/null || true)"
      if [ -n "\$pid" ] && kill -0 "\$pid" 2>/dev/null; then
        echo "[huri] stopping \$svc (pid \$pid)"
        kill "\$pid" 2>/dev/null || true
      fi
      rm -f "\$pidfile"
    fi
  done
fi
echo "[huri] stopped"
EOF
  run chmod +x "$STATE_DIR/stop.sh"

  local llm_probe_path="/v1/models"
  [[ "$LLM_PROVIDER" == "ollama" ]] && llm_probe_path="/api/tags"
  write_file "$STATE_DIR/status.sh" <<EOF
#!/usr/bin/env bash
# Show the state of every HuRI dependency, local or remote.
set -Euo pipefail
cd "$REPO_ROOT"
source "$STATE_DIR/env.sh"
# shellcheck disable=SC1091
[ -r "$STATE_DIR/secrets.env" ] && source "$STATE_DIR/secrets.env"

CURL=(curl -fsS --max-time 5$( ((VERIFY_SSL)) || printf ' -k'))
AUTH=()
[ -n "\${HURI_LLM_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer \${HURI_LLM_API_KEY}")

probe() {
  local label="\$1"; shift
  if "\$@" >/dev/null 2>&1; then printf '%-10s up\n' "\$label"
  else printf '%-10s down\n' "\$label"; fi
}

probe qdrant "\${CURL[@]}" "${QDRANT_URL%/}/readyz"
probe llm    "\${CURL[@]}" "\${AUTH[@]}" "${LLM_URL%/}${llm_probe_path}"
probe ray    ray status

# The Serve HTTP proxy answers /-/healthz as soon as it is up, which is true
# even when huri-app is DEPLOY_FAILED and no deployment exists — so probing it
# reported "huri up" on a completely broken stack. Read the application status.
huri_state="\$(serve status --name huri-app 2>/dev/null | awk '/^status:/ {print \$2; exit}')"
case "\${huri_state:-}" in
  RUNNING) printf '%-10s up\n'   huri ;;
  "")      printf '%-10s down       (not deployed)\n' huri ;;
  *)       printf '%-10s down       (%s)\n' huri "\$huri_state" ;;
esac
echo
serve status 2>/dev/null || true
EOF
  run chmod +x "$STATE_DIR/status.sh"
  ok "run scripts → .huri-local/{start,stop,status}.sh"

  # Keep generated artefacts out of git.
  local gi="$REPO_ROOT/.gitignore"
  local entry
  for entry in "/.huri-local/" "/assets/" "config/*.generated.yaml" \
               "/qdrant_storage/" "/.venv*/" "/uv.lock"; do
    if ! grep -qxF "$entry" "$gi" 2>/dev/null; then
      (( DRY_RUN )) || printf '%s\n' "$entry" >>"$gi"
    fi
  done
}

# =============================================================================
# 8. Stage: verification
# =============================================================================

verify() {
  stage_enabled verify || return 0
  step "Verification"
  (( DRY_RUN )) && { note "skipped in --dry-run"; return 0; }

  local failures=0 out

  # detail <text> — print a captured error block as indented notes, so the real
  # exception ends up both on screen and in install.log.
  detail() {
    local line
    while IFS= read -r line; do [[ -n "$line" ]] && note "$line"; done <<<"$1"
  }

  # ctranslate2 is the actual STT engine (faster-whisper is a thin wrapper) and
  # webrtcvad/sounddevice are the fragile VAD deps — one builds from sdist, the
  # other dlopens libportaudio. All three were missing from this list, which is
  # why an install could "succeed" with a dead speech pipeline.
  local checks="ray,faster_whisper,ctranslate2,qdrant_client,httpx,numpy,webrtcvad"
  [[ "$P_MODULES" == *vad* ]] && checks="$checks,sounddevice,scipy,omegaconf"
  [[ "$GPU_VENDOR" != "none" || "$P_TTS_DEV" != "off" || "$P_GES_DEV" != "off" || "$P_EMO_DEV" != "off" ]] \
    && checks="$checks,torch"

  if out="$(PYTHONWARNINGS=ignore "$VPY" - "$checks" 2>&1 <<'PY'
import importlib, sys
missing = []
for name in sys.argv[1].split(","):
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{name}: {type(exc).__name__}: {exc}")
if missing:
    print("\n".join(missing)); sys.exit(1)
PY
)"; then
    ok "core imports ($checks)"
  else
    err "core imports failed:"
    detail "$out"
    # Keyed on the symptom, not on $GPU_VENDOR: by the time verification runs the
    # planner may already have degraded the vendor to "none" (that is precisely
    # the situation where this hint matters most).
    if [[ "$out" == *"cannot open shared object file"* ]] \
       && [[ "$out" =~ lib(amdhip|hip|roc|rccl|MIOpen) ]]; then
      note ""
      note "Those are ROCm libraries. The pip wheels link the ROCm runtime"
      note "dynamically and bundle none of it, so it has to come from the distro:"
      rocm_runtime_hint
    fi
    failures=1
  fi

  # The registry is what turns HURI_MODULES into Serve deployments; if it cannot
  # be built, `serve deploy` fails later with a much less obvious error.
  if out="$(cd "$REPO_ROOT" && HURI_MODULES="$P_MODULES" PYTHONWARNINGS=ignore \
            "$VPY" -c 'import src.modules.modules as m; print(len(m.get_modules()), "modules registered")' 2>&1 | tail -1)"; then
    ok "module registry builds: $out"
  else
    err "module registry failed to build (HURI_MODULES=$P_MODULES):"
    detail "$out"
    failures=1
  fi

  if [[ "$GPU_VENDOR" != "none" ]]; then
    # Separate "torch does not import" from "torch sees no GPU". Collapsing the
    # two (the old `2>/dev/null` on an is_available() probe) reported a missing
    # ROCm runtime as "cannot see the GPU" and threw the traceback away.
    local tinfo
    if ! tinfo="$("$VPY" -c 'import torch; print("build:", torch.version.hip or torch.version.cuda or "cpu", "| available:", torch.cuda.is_available(), "| devices:", torch.cuda.device_count())' 2>&1)"; then
      err "torch does not import:"
      detail "$tinfo"
      [[ "$GPU_VENDOR" == "amd" ]] && rocm_runtime_hint
      failures=1
    elif [[ "$tinfo" != *"available: True"* ]]; then
      err "torch imports but sees no GPU — $tinfo"
      [[ "$GPU_VENDOR" == "amd" ]] && note "check /dev/kfd permissions and that you are in the 'render' group"
      failures=1
    else
      ok "torch sees the GPU — $tinfo"
      # Seeing the GPU is not the same as being able to run on it: a wheel built
      # without this card's gfx target reports the device happily and then
      # aborts inside HIP on the first kernel launch. Actually launch one.
      local karch
      karch="$("$VPY" -c 'import torch;print(torch.cuda.get_device_properties(0).gcnArchName.split(":")[0], "|", ",".join(torch.cuda.get_arch_list()))' 2>&1 || true)"
      if out="$("$VPY" -c '
import torch
a = torch.randn(256, 256, device="cuda", dtype=torch.float16)
torch.cuda.synchronize()
print("kernel launch ok:", float((a @ a).float().sum()) == float((a @ a).float().sum()))
' 2>&1)"; then
        ok "GPU kernels run — $karch"
      else
        err "torch sees the GPU but cannot launch a kernel on it:"
        detail "$(printf '%s\n' "$out" | tail -4)"
        note "arch / built-for: $karch"
        if [[ "$GPU_VENDOR" == "amd" ]]; then
          note "this card's gfx target is likely absent from the installed torch build."
          note "Re-run 'scripts/install_local.sh --only config' to set HSA_OVERRIDE_GFX_VERSION,"
          note "or install a torch built for it (download.pytorch.org ROCm wheels cover"
          note "consumer RDNA3; the repo.radeon.com '.lw.' wheels do not)."
        fi
        failures=1
      fi
    fi
  fi

  # STT is the one GPU component on the ROCm path, and it runs on CTranslate2,
  # not torch — so a torch-only probe says nothing about whether STT works.
  if [[ "$P_STT_DEV" == "gpu" ]]; then
    local ct2n
    ct2n="$("$VPY" -c 'import ctranslate2; print(ctranslate2.get_cuda_device_count())' 2>&1)" || true
    if [[ "$ct2n" =~ ^[0-9]+$ ]] && (( ct2n > 0 )); then
      ok "CTranslate2 sees $ct2n GPU(s)"
    else
      err "STT is planned on the GPU but CTranslate2 reports no usable device:"
      detail "$ct2n"
      note "re-run with --profile cpu for a CPU STT install, or fix the GPU runtime"
      failures=1
    fi
  fi

  # Actually load the model. Everything above can pass while STT still fails at
  # the first utterance (wrong compute type, truncated snapshot, dead device).
  local whisper_dir="$MODELS_DIR/whisper/${WHISPER_REPO_PREFIX}-${STT_SIZE}"
  if [[ "$P_MODULES" == *stt* && -f "$whisper_dir/model.bin" ]]; then
    local stt_dev stt_ct
    stt_dev="$(stt_ct2_device)"; stt_ct="$(stt_ct2_compute_type)"
    if out="$(PYTHONWARNINGS=ignore "$VPY" - "$whisper_dir" "$stt_dev" "$stt_ct" 2>&1 <<'PY'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device=sys.argv[2], compute_type=sys.argv[3])
print("loaded", sys.argv[1].rsplit("/", 1)[-1], "on", sys.argv[2], sys.argv[3])
PY
)"; then
      ok "STT model loads: $out"
    else
      err "STT model failed to load (device=$stt_dev compute_type=$stt_ct):"
      detail "$out"
      failures=1
    fi
  fi

  if [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "piper" ]]; then
    local piper_onnx="$MODELS_DIR/piper/$PIPER_VOICE_NAME.onnx"
    if out="$(PYTHONWARNINGS=ignore "$VPY" - "$piper_onnx" <<'PY' 2>&1
import sys, time
import numpy as np
from piper import PiperVoice
v = PiperVoice.load(sys.argv[1])
t0 = time.time()
n = sum(np.asarray(c.audio_float_array).size for c in v.synthesize("HuRI is ready."))
dur = n / v.config.sample_rate
print(f"{dur:.2f}s audio in {time.time()-t0:.2f}s "
      f"({(time.time()-t0)/dur:.3f}x realtime) @ {v.config.sample_rate}Hz")
PY
)"; then
      ok "piper synthesises: $out"
    else
      err "piper failed to synthesise:"
      detail "$(printf '%s\n' "$out" | tail -5)"
      note "voice model expected at $piper_onnx"
      note "re-download: $VPY -m piper.download_voices --download-dir $MODELS_DIR/piper $PIPER_VOICE_NAME"
      failures=1
    fi
  fi

  if [[ "$P_TTS_DEV" != "off" && "$TTS_ENGINE" == "cosyvoice" ]]; then
    # Capture the traceback instead of discarding it with 2>/dev/null. The
    # failure is almost never "check your submodules" — it is a missing
    # transitive dependency (lightning/gdown/wget via Matcha-TTS, pyworld via
    # the HyperPyYAML pydoc.locate of cosyvoice.dataset.processor), and the
    # exception names it exactly.
    if out="$(PYTHONPATH="$ASSETS_DIR/cosyvoice:$ASSETS_DIR/cosyvoice/third_party/Matcha-TTS" \
              PYTHONWARNINGS=ignore \
              "$VPY" -c 'from cosyvoice.cli.cosyvoice import CosyVoice3' 2>&1)"; then
      ok "CosyVoice3 importable"
    else
      err "CosyVoice3 import failed:"
      detail "$(printf '%s\n' "$out" | tail -6)"
      note "if a module is missing, it was dropped by generate_cpu_requirements()"
      failures=1
    fi

    # Unlike the Kubernetes deploy (where the voice sample PVC is deliberately
    # populated *after* first install, via `kubectl cp`), a bare-metal install
    # has no later provisioning step: if the file is not here now, it never
    # will be, and every TTS session will fail per-request (each error is
    # logged but swallowed by the event graph, so the client just gets no
    # audio — see src/core/events.py::EventGraph._run). Treat it as a hard
    # requirement rather than the best-effort skip TTSHandle allows.
    if [[ -f "$ASSETS_DIR/voice.wav" ]]; then
      ok "voice sample present at $ASSETS_DIR/voice.wav"
    else
      err "no $ASSETS_DIR/voice.wav — TTS is enabled but has no reference voice"
      note "Add one with: scripts/install_local.sh --only models,config \\"
      note "    --voice-sample /path/to/voice.wav --voice-transcript 'exact words spoken'"
      failures=1
    fi
  fi

  if [[ "$P_GES_DEV" != "off" ]]; then
    if (cd "$REPO_ROOT" && "$VPY" -c 'from src.modules.gesture.emage import EmageAudioModel' 2>/dev/null); then
      ok "EMAGE importable"
    else
      err "EMAGE import failed"; failures=1
    fi
  fi

  if (( ! SKIP_SERVICES )); then
    local qargs=(); (( VERIFY_SSL )) || qargs+=(-k)
    if curl -fsS --max-time 5 "${qargs[@]}" "${QDRANT_URL%/}/readyz" >/dev/null 2>&1; then
      ok "qdrant reachable at $QDRANT_URL"

      # 'conversations' is created on demand by RAGHandle, but the document
      # collection only ever comes from the ingestion CLI. Missing, every
      # retrieval returns nothing and the answers are merely ungrounded — no
      # error anywhere. Report it here so it is known at install time.
      local cols
      cols="$(curl -fsS --max-time 5 "${qargs[@]}" "${QDRANT_URL%/}/collections" 2>/dev/null \
              | tr ',' '\n' | sed -nE 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' \
              | paste -sd, - || true)"
      if [[ ",$cols," == *",documents,"* ]]; then
        ok "qdrant has the 'documents' collection"
      else
        warn "qdrant has no 'documents' collection — document retrieval will return nothing"
        note "collections present: ${cols:-<none>}"
        note "Ingest some: source .huri-local/env.sh && python -m src.modules.rag.ingestion --help"
        note "(conversational memory still works — RAGHandle creates it on demand)"
      fi
    elif (( QDRANT_REMOTE )); then
      warn "qdrant at $QDRANT_URL did not answer — RAG memory will fail at runtime"
    else
      warn "qdrant not reachable (start.sh will launch it)"
    fi

    if (( LLM_REMOTE )); then
      probe_remote "llm" "$LLM_URL"
      (( EMBED_REMOTE )) && [[ "$EMBED_URL_EFF" != "$LLM_URL" ]] && probe_remote "embeddings" "$EMBED_URL_EFF"
    elif curl -fsS --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
      ok "ollama reachable on :11434"
      curl -fsS --max-time 3 http://localhost:11434/api/tags | grep -q "${LLM_MODEL%%:*}" \
        && ok "LLM $LLM_MODEL present" || warn "LLM $LLM_MODEL not pulled yet"
    else
      warn "ollama not reachable (start.sh will launch it)"
    fi
  fi

  return $failures
}

# =============================================================================
# main
# =============================================================================

main() {
  mkdir -p "$STATE_DIR"
  : >>"$LOG_FILE"

  printf '%s\n' "${C_B}HuRI local installer${C_RST} — $REPO_ROOT"
  (( DRY_RUN )) && note "dry run: nothing will be installed"

  detect_os
  detect_host
  detect_gpu
  detect_tools
  local py_ok=1
  detect_python || py_ok=0
  report_detection

  if (( ! py_ok )); then
    echo
    if [[ "$PKG_MGR" == "pacman" ]]; then
      err "no suitable Python found (need 3.10–3.12)"
      note "Arch/EndeavourOS only ship the current python via pacman, which is usually"
      note "newer than 3.12 — there is no 3.10–3.12 package in the official repos."
      note "Install one via pyenv/uv, or the AUR (e.g. 'yay -S python312'), then re-run"
      note "with --python /path/to/python3.12"
      die "no suitable Python found"
    elif [[ "$PKG_MGR" == "apt" ]]; then
      err "no suitable Python found (need 3.10–3.12)"
      note "Ubuntu 24.04: sudo apt install python3.12 python3.12-venv python3.12-dev"
      note "Ubuntu 22.04: 3.12 is not in the archive — add deadsnakes first:"
      note "  sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update"
      note "  sudo apt install python3.12 python3.12-venv python3.12-dev"
      note "Then re-run with --python /usr/bin/python3.12"
      die "no suitable Python found"
    else
      die "no suitable Python found (need 3.10–3.12; pass --python /path/to/python3.12)"
    fi
  fi

  resolve_endpoints
  plan
  report_plan
  save_plan

  if (( PLAN_ONLY )); then
    echo
    note "plan written to .huri-local/plan.env — re-run without --plan-only to install"
    return 0
  fi

  echo
  check_hard_limits || die "the machine cannot host this plan"

  if ! confirm "Proceed with the install described above?"; then
    info "aborted — nothing was changed"
    return 0
  fi

  install_system_packages
  install_python_deps
  download_models
  install_services
  generate_configs
  verify || die "verification failed — see the ✗ lines above, fix them, then re-run (e.g. with --only <stage>)"

  step "Done"
  cat <<EOF
    Start:   .huri-local/start.sh
    Status:  .huri-local/status.sh
    Client:  source .huri-local/env.sh
             python -m src.client --config config/client_local.generated.yaml
    Stop:    .huri-local/stop.sh [--all]

    Plan:    .huri-local/plan.env          Log: .huri-local/install.log
EOF
}

main "$@"
