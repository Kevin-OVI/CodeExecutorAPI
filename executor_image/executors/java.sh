#!/bin/bash
set -euo pipefail

printf '%s' "$1" > /tmp/Main.java
javac -encoding UTF-8 /tmp/Main.java

java -cp /tmp Main
