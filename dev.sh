#!/usr/bin/env bash
# Run a command inside the pinned Conan 1.66 dev container.
#   ./dev.sh build            build the image
#   ./dev.sh <cmd...>         run <cmd> in the container with this dir mounted at /work
#   ./dev.sh                  interactive shell
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="conan-utils-dev"

if [[ "${1:-}" == "build" ]]; then
    rev="$(git -C "$here" describe --always --dirty 2>/dev/null || echo unknown)"
    BUILDAH_FORMAT=docker podman build \
        --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --build-arg GIT_REVISION="$rev" \
        -t "$image" "$here"
    exit 0
fi

# --userns=keep-id maps the host uid onto the container's 'dev' user (uid 1000).
tty_flags=(); [[ -t 0 && -t 1 ]] && tty_flags=(-it)
podman run --rm "${tty_flags[@]}" --userns=keep-id -v "$here:/work:Z" -w /work "$image" "${@:-bash}"
