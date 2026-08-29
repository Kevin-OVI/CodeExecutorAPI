#!/bin/sh
set -euo pipefail

printf '%s' "$1" > /tmp/main.cpp
g++ -fdiagnostics-color=never /tmp/main.cpp -o /tmp/main
chmod +x /tmp/main

/tmp/main
