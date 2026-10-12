#!/usr/bin/env bash
# CosyVoice has no setup.py/pyproject.toml, so pip cannot install it, not even
# from git. Clone it at the pinned commit and put it on PYTHONPATH instead:
#
#   packages/huri-module-tts-cosyvoice/install_cosyvoice.sh /opt/cosyvoice
#   export PYTHONPATH="/opt/cosyvoice:/opt/cosyvoice/third_party/Matcha-TTS:$PYTHONPATH"
#
# Keep COSYVOICE_COMMIT in step with deploy/Dockerfile.nvidia and
# scripts/install_local.sh.
set -euo pipefail
COSYVOICE_REPO="https://github.com/FunAudioLLM/CosyVoice.git"
COSYVOICE_COMMIT="074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
dest="${1:?usage: install_cosyvoice.sh DEST_DIR}"

if [[ ! -d "$dest/.git" ]]; then
  git clone "$COSYVOICE_REPO" "$dest"
fi
git -C "$dest" fetch --quiet origin "$COSYVOICE_COMMIT" || true
git -C "$dest" checkout --quiet "$COSYVOICE_COMMIT"
git -C "$dest" submodule update --init --recursive
echo "CosyVoice $COSYVOICE_COMMIT at $dest"
echo "PYTHONPATH=$dest:$dest/third_party/Matcha-TTS"
