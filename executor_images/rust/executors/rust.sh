#!/bin/sh
set -euo pipefail

printf '%s' "$1" > /tmp/main.rs
rustc /tmp/main.rs -o /tmp/main
/tmp/main
