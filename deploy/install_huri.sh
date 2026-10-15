#!/bin/sh
# Installs HuRI into an image: huri-core plus every distribution under
# packages/ (the curated events and modules), all from the build context.
#
#   install_huri.sh SRC_DIR
#
# --no-deps: each image installs the heavy, hardware-specific dependencies
# first, from its own requirement files; these packages only add the code.
# Every curated module is installed, whatever the image's hardware: the head
# node imports every enabled module class to build the app and bind its
# deployments (module code imports its heavy dependencies lazily), while
# HURI_MODULES and the Serve config decide what actually runs where.
#
# The build fails here, rather than at deploy time, if a module or event does
# not load or the installed versions do not fit together.
set -eu
src="${1:?usage: install_huri.sh SRC_DIR}"

pip install --no-cache-dir --no-deps "$src" "$src"/packages/*/

python - <<'PY'
from huri_core.core.compat import check_manifests, check_modules
from huri_core.core.plugins import (
    find_manifest,
    installed_modules,
    load_events,
    load_modules,
)

events = load_events()
modules = load_modules(installed_modules())
check_modules(modules, events)
manifests = {name: find_manifest(name) for name in modules}
missing = sorted(name for name, manifest in manifests.items() if manifest is None)
if missing:
    raise SystemExit(f"curated module(s) without a huri_module.toml: {missing}")
check_manifests(modules, manifests, events)
print(f"HuRI: {len(events)} event topics, modules {sorted(modules)}")
PY
