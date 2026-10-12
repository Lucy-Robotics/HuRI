#!/usr/bin/env bash
# Turns the template into your module:  ./rename.sh servo
# Renames example/Example in every file and directory: huri_example ->
# huri_servo, huri-module-example -> huri-module-servo, the "example." topic
# namespace -> "servo.", and so on. Run it once, on a fresh copy.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
name="${1:?usage: rename.sh <name>   (lowercase, e.g. servo)}"
[[ "$name" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "name must be lowercase [a-z0-9_]" >&2; exit 2; }
Name="$(tr '[:lower:]' '[:upper:]' <<<"${name:0:1}")${name:1}"

files=$(grep -rlI --exclude-dir=.git --exclude=rename.sh -e example -e Example .)
for f in $files; do
  sed -i -e "s/example/$name/g" -e "s/Example/$Name/g" "$f"
done
mv huri_example "huri_$name"
mv events/huri_events_example "events/huri_events_$name"
echo "renamed to $name: edit huri_$name/module.py, its manifest and tests"
