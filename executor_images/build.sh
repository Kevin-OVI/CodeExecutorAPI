#!/usr/bin/env bash
# Build the sandbox images in parallel, then warm each one for the uid mapping the API runs it
# with (see "Requirements" in the README for why the warm-up cannot be skipped). Run it as the
# user that runs the API, with the same PODMAN_IMAGE_PREFIX and CONTAINER_USER_ID.
#
# Usage: executor_images/build.sh [environment...]
#
# With no argument every environment is built; naming some (`build.sh rust gcc`) builds and warms
# only those. The two images in base/ are always built first, since everything else starts from
# them - a cache hit when nothing in base/ changed.
set -euo pipefail

cd "$(dirname "$0")"

prefix="${PODMAN_IMAGE_PREFIX:-code_executor}"
uid="${CONTAINER_USER_ID:-4000}"
python_image="docker.io/library/python:3.15.0-trixie"

# Every image the API runs, by the suffix `code_executor_api/languages.py` gives it. `bash` is
# base/ built on top of $python_image; each other one is the directory of the same name.
environments=(bash python node gcc java dotnet rust)

if (($#)); then
  for environment in "$@"; do
    if [[ " ${environments[*]} " != *" $environment "* ]]; then
      echo "Unknown environment '$environment' (expected one of: ${environments[*]})" >&2
      exit 2
    fi
  done
  environments=("$@")
fi

job_pids=()
job_names=()

# Run a command in the background, prefixing each line of its output with the job's name so the
# interleaved logs of parallel builds stay readable. `pipefail` carries into the subshell, so the
# job fails when the command does, not when `sed` succeeds.
start_job() {
  local name=$1
  shift
  ("$@" 2>&1 | sed -u "s/^/[$name] /") &
  job_pids+=("$!")
  job_names+=("$name")
}

# Wait for every started job - not just until the first failure, so no build is left running
# unattended - then stop the script if any of them failed.
wait_jobs() {
  local failed=() i
  for i in "${!job_pids[@]}"; do
    wait "${job_pids[$i]}" || failed+=("${job_names[$i]}")
  done
  job_pids=()
  job_names=()
  if ((${#failed[@]})); then
    echo "Failed: ${failed[*]}" >&2
    exit 1
  fi
}

start_job base podman build -t "$prefix-base" base
start_job bash podman build --build-arg FROM_IMAGE="$python_image" -t "$prefix-bash" base
wait_jobs

for environment in "${environments[@]}"; do
  case $environment in
    bash) ;;
    python) start_job "$environment" podman build --build-arg BASE_IMAGE="$prefix-bash" -t "$prefix-$environment" "$environment" ;;
    *) start_job "$environment" podman build --build-arg BASE_IMAGE="$prefix-base" -t "$prefix-$environment" "$environment" ;;
  esac
done
wait_jobs

for environment in "${environments[@]}"; do
  start_job "warm $environment" podman run --rm --userns="keep-id:uid=$uid,gid=$uid" "$prefix-$environment" true
done
wait_jobs

echo "Built and warmed: ${environments[*]/#/$prefix-}"
