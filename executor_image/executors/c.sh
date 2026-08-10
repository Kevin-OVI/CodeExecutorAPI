#!/bin/bash
set -euo pipefail

printf '%s' "$1" > /tmp/main.c
gcc -fdiagnostics-color=never /tmp/main.c -o /tmp/main
chmod +x /tmp/main

/tmp/main
