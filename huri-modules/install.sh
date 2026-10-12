#!/usr/bin/env bash
# Installs the curated modules on this machine: probes the hardware, plans
# which modules fit, installs them with the right variant of their
# dependencies and writes the matching server and client configs. Every flag
# of scripts/install_local.sh applies (--plan-only, --modules, --tts-engine...).
#
# While huri-modules lives inside the HuRI repository this delegates to the
# installer next door; it becomes that installer once this directory is its
# own repository (see README.md).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/../scripts/install_local.sh" "$@"
