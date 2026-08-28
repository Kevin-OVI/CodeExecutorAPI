#!/bin/sh
set -euo pipefail

printf '%s' "$1" > /tmp/Program.cs

dotnet /tmp/Program.cs
