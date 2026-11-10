#!/bin/bash
# Run every test: Python, the installer, then the addon's Lua tests with every Lua you have
# (WoW itself runs Lua 5.1, so lua5.1 or luajit are the most faithful; plain lua 5.4 also works).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -W error::ResourceWarning -m unittest discover -s tests -t . -v
python3 tests/make_sample.py
bash scripts/test_install.sh
found=0
for LUA in lua5.1 luajit lua; do
  command -v "$LUA" >/dev/null 2>&1 || continue
  found=1
  for t in tests/test_*.lua; do
    echo "== $LUA $t"
    "$LUA" "$t"
  done
done
if [ "$found" = 0 ]; then echo "(skipping Lua tests: install lua5.1 or luajit)"; exit 0; fi
echo "all tests passed"
