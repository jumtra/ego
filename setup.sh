#!/bin/sh
# Simple, offline onboarding. From the ZIP: ./setup.sh [project-path] [setup flags]
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -gt 0 ]; then
  case "$1" in
    -*) target="$(pwd)" ;;
    *) target="$1"; shift ;;
  esac
else
  target="$(pwd)"
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.11+ is required to run SEH. Install Python first.' >&2
  exit 3
fi
exec python3 "$base/run_seh.py" setup --root "$target" "$@"
