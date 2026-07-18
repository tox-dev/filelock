#!/usr/bin/env bash
# Run the filelock test suite inside a real Termux (Android/bionic) userland via tox. Termux's CPython ships without
# os.link and reports sys.platform == "android"; this keeps that platform honest. tox itself imports filelock, so the
# patched tree is installed first, otherwise tox would crash on startup with the very AttributeError this fixes.
set -euo pipefail

apt-get update -qq
apt-get install -yq python python-pip git

work="$HOME/work"
mkdir -p "$work"
cp -a /repo/. "$work/"
cd "$work"
git config --global --add safe.directory "$work"

pip install -q tox
pip install -q .

exec tox run -e py "$@"
