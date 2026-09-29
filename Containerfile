# Development / runtime container for the conan utilities.
# Pinned to Conan 1.66 (the version we target). Python 3.11 is the newest
# version Conan 1.x was released against; it also runs on newer Pythons.
FROM docker.io/library/python:3.11-slim-bookworm

ARG BUILD_DATE
ARG GIT_REVISION
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_REVISION}"
RUN printf 'build_date=%s\ngit_revision=%s\n' "${BUILD_DATE}" "${GIT_REVISION}" > /etc/build-info

RUN pip install --no-cache-dir --root-user-action=ignore \
        "conan==1.66.0" "cyclonedx-python-lib[json-validation]" pytest ruff shellcheck-py spdx-tools uv

# Non-root user so files created in the mounted workdir are owned by the caller.
RUN useradd -m -u 1000 dev
USER dev
WORKDIR /work
ENV CONAN_USER_HOME=/home/dev PATH=/home/dev/.local/bin:$PATH
