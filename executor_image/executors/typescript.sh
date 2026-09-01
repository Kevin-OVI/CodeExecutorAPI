#!/bin/sh
set -euo pipefail

printf '%s' "$1" > /tmp/main.ts
tsc --noEmit --esModuleInterop --resolveJsonModule --strict /tmp/main.ts
npx tsx /tmp/main.ts
