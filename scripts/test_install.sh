#!/usr/bin/env bash
# =============================================================================
# HuRI — installer portability test
# =============================================================================
#
# Runs scripts/install_local.sh inside clean containers so "it installs on
# Ubuntu" is something we check rather than something we claim. Uses distrobox
# when available (it shares $HOME, so the repo is visible without a bind mount),
# otherwise plain docker/podman.
#
#   scripts/test_install.sh                 # plan-only on every target (fast)
#   scripts/test_install.sh --full          # plan + a real CPU install + verify
#   scripts/test_install.sh --targets u2404 # just one target
#   scripts/test_install.sh --keep          # leave the containers for poking at
#
# The repo is COPIED into a scratch directory per target first: a real install
# writes .huri-local/ and config/*.generated.yaml, and running that against your
# working tree would clobber the setup you are using.
#
# GPU paths are deliberately not covered — containers here have no GPU, and the
# GPU logic is what needs a human with the hardware in front of them.
# =============================================================================

set -Eeuo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_PATH")")"
WORK_BASE="${HURI_TEST_WORKDIR:-$HOME/.cache/huri-install-test}"

FULL=0
KEEP=0
TARGETS="u2404 u2204"

if [[ -t 1 ]]; then
  C_RST=$'\033[0m'; C_B=$'\033[1m'; C_R=$'\033[31m'; C_G=$'\033[32m'; C_C=$'\033[36m'
else
  C_RST=; C_B=; C_R=; C_G=; C_C=
fi
ok()   { printf '  %s✓%s %s\n' "$C_G" "$C_RST" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$C_R" "$C_RST" "$*" >&2; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_C" "$C_RST" "$C_B" "$*" "$C_RST"; }

usage() {
  sed -n '3,21p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)    FULL=1 ;;
    --keep)    KEEP=1 ;;
    --targets) TARGETS="${2:?}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

image_for() {
  case "$1" in
    u2404) echo "ubuntu:24.04" ;;
    u2204) echo "ubuntu:22.04" ;;
    arch)  echo "archlinux:latest" ;;
    *)     return 1 ;;
  esac
}

# Ubuntu 22.04 ships python 3.10 and 24.04 ships 3.12 — both supported, so each
# target pins its own interpreter rather than letting detection find whatever
# leaked in from a shared $HOME (distrobox mounts it, so a host uv/pyenv python
# is visible inside the container and would invalidate the test).
python_for() {
  case "$1" in
    u2404) echo "/usr/bin/python3.12" ;;
    u2204) echo "/usr/bin/python3" ;;
    arch)  echo "" ;;
    *)     echo "" ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

RUNNER=""
if have distrobox; then RUNNER="distrobox"
elif have docker && docker info >/dev/null 2>&1; then RUNNER="docker"
elif have podman; then RUNNER="podman"
else
  bad "need distrobox, docker or podman"
  exit 1
fi

prepare_workdir() { # prepare_workdir <target> -> echoes the path
  local target="$1" dir="$WORK_BASE/$target"
  # Guard the rm: this runs unattended and HURI_TEST_WORKDIR is user-supplied.
  # Refuse anything that is not a scratch path we would have created ourselves.
  case "$dir" in
    */huri-install-test/*) ;;
    *) echo "refusing to clear '$dir' — HURI_TEST_WORKDIR must end in /huri-install-test" >&2
       exit 2 ;;
  esac
  [[ -d "$dir" ]] && rm -rf "$dir"
  mkdir -p "$dir"
  # Only tracked files: no .venv, no assets/ (9+ GiB), no .huri-local.
  ( cd "$REPO_ROOT" && git ls-files -z | tar --null -T - -cf - ) | tar -xf - -C "$dir"
  printf '%s\n' "$dir"
}

box_name() { printf 'huri-test-%s\n' "$1"; }

run_in_box() { # run_in_box <target> <script>
  local target="$1" script="$2" image box
  image="$(image_for "$target")"
  box="$(box_name "$target")"
  case "$RUNNER" in
    distrobox)
      distrobox create --name "$box" --image "$image" --yes >/dev/null 2>&1 || true
      distrobox enter "$box" -- bash -lc "$script"
      ;;
    docker|podman)
      "$RUNNER" run --rm -v "$WORK_BASE:$WORK_BASE" -w "$WORK_BASE" \
        -e HOME="$WORK_BASE" "$image" bash -lc "$script"
      ;;
  esac
}

cleanup_box() {
  local box; box="$(box_name "$1")"
  (( KEEP )) && { ok "kept container $box"; return 0; }
  [[ "$RUNNER" == "distrobox" ]] && distrobox rm --force "$box" >/dev/null 2>&1 || true
}

failures=0
for target in $TARGETS; do
  image="$(image_for "$target")" || { bad "unknown target '$target'"; failures=1; continue; }
  step "$target ($image)"

  dir="$(prepare_workdir "$target")"
  py="$(python_for "$target")"
  py_arg=""
  [[ -n "$py" ]] && py_arg="--python $py"

  # 1. Plan only — exercises OS/python/package detection and the whole planner
  #    without installing anything. This is the cheap portability signal.
  if run_in_box "$target" "cd '$dir' && ./scripts/install_local.sh --plan-only $py_arg" \
       >"$dir/plan.log" 2>&1; then
    verdict="$(grep -oE 'VERDICT: .*' "$dir/plan.log" | head -1)"
    ok "plan ok — ${verdict:-no verdict line}"
    grep -qE '^\s+tts\s+piper' "$dir/plan.log" \
      && ok "piper TTS planned (voice out works with no GPU)" \
      || { bad "piper TTS was NOT planned"; failures=1; }
  else
    bad "plan failed — see $dir/plan.log"
    tail -15 "$dir/plan.log" >&2
    failures=1
    cleanup_box "$target"
    continue
  fi

  # 2. A real CPU install + verify. Slow (multi-GiB of wheels) and network
  #    bound, so opt-in.
  if (( FULL )); then
    if run_in_box "$target" "cd '$dir' && ./scripts/install_local.sh -y --profile cpu \
         $py_arg --venv '$dir/.venv' --skip-services" >"$dir/install.log" 2>&1; then
      ok "install + verify ok"
      grep -oE '✓ (core imports|piper synthesises|STT model loads)[^\n]*' "$dir/install.log" \
        | sed 's/^/      /' || true
    else
      bad "install failed — see $dir/install.log"
      grep -E '✗|Error|error:' "$dir/install.log" | tail -12 >&2 || tail -20 "$dir/install.log" >&2
      failures=1
    fi
  fi

  cleanup_box "$target"
done

step "Result"
if (( failures )); then
  bad "$failures target(s) failed"
  exit 1
fi
ok "all targets passed"
printf '    logs under %s/<target>/\n' "$WORK_BASE"
