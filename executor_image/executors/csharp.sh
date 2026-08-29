#!/bin/sh
set -euo pipefail

export HOME=/tmp

{
  printf '#:property PublishAot=false\n'
  printf '%s' "$1"
} > /tmp/Program.cs

dotnet /tmp/Program.cs
