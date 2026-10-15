#!/usr/bin/env bash
# Installs this module with the dependency variant that matches the machine:
# requirements-nvidia.txt on NVIDIA, requirements-amd.txt on ROCm, else
# requirements-cpu.txt; then the events package and the module itself.
#
#   ./install.sh                # probe the hardware
#   ./install.sh --variant cpu  # or choose
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

variant=""
if [[ "${1:-}" == "--variant" ]]; then variant="${2:?cpu|nvidia|amd}"; fi
if [[ -z "$variant" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    variant=nvidia
  elif [[ -e /dev/kfd ]] && command -v rocminfo >/dev/null 2>&1; then
    variant=amd
  else
    variant=cpu
  fi
fi
echo "installing for: $variant"

pip="${PIP:-python -m pip}"
if grep -qvE '^\s*(#|$)' "requirements-$variant.txt" 2>/dev/null; then
  $pip install -r "requirements-$variant.txt"
fi
$pip install -e ./events -e ".[dev]"
