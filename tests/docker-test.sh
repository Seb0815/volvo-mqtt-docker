#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project="volvo-mqtt-test-$$"

cleanup() {
    status=$?
    trap - EXIT
    if ! docker compose -p "$project" -f "$root/compose.test.yaml" down --volumes; then
        echo "Docker-Testressourcen konnten nicht vollstaendig entfernt werden." >&2
        status=1
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose -p "$project" -f "$root/compose.test.yaml" up \
    --build --abort-on-container-exit --exit-code-from tests
