#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
example_python=${OPENAGENT_EXAMPLE_PYTHON:-python3}

exec "$example_python" "$script_dir/setup_example.py" "$@"

