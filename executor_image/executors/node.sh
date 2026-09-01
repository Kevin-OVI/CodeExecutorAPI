#!/bin/sh
set -euo pipefail

printf '%s' "$1" > /tmp/main.js
node /tmp/main.js
