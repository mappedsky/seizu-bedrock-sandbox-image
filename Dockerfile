# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183

# A seizu-sandbox/1 image for Amazon Bedrock AgentCore Runtime. Every input is
# pinned: the base image and uv by digest, Python packages by hash from uv.lock.
# Nothing is installed from apt.

FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.13-slim-trixie@sha256:1b5c0692836eae9b028cc352d7146872f36939aa1fb0de24d3ef6df307b956c9

# The libraries sandboxed code can import (the `sandbox` group in
# pyproject.toml), wheels only, each checked against its hash in uv.lock. uv is
# mounted for the build and is not in the image.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=/build/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/build/uv.lock \
    --mount=type=tmpfs,target=/tmp \
    uv export --project /build --frozen --only-group sandbox --no-emit-project \
        --format requirements-txt --output-file /tmp/requirements.txt \
 && uv pip install --python /usr/local/bin/python3 --system --no-cache \
        --require-hashes --no-deps --only-binary :all: --compile-bytecode \
        --requirement /tmp/requirements.txt

# $HOME is a symlink into the workspace mount, so everything Seizu writes under
# /home/user persists between turns. The mount is attached per invocation; the
# directory here only stands in for it when the image runs without one.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /home/user --shell /bin/bash user \
 && install -d -o 1000 -g 1000 -m 0750 /mnt/workspace \
 && ln -s /mnt/workspace /home/user \
 && install -d -m 0755 /opt/seizu-sandbox

COPY --chmod=0644 sandbox/server.py sandbox/kernel.py /opt/seizu-sandbox/

ENV HOME=/home/user \
    LANG=C.UTF-8 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/.cache \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 1000:1000

# Fails the build if a library does not import, and builds matplotlib's font
# cache into the image instead of into every session's first plot.
RUN python3 -c "import matplotlib.pyplot, numpy, openpyxl, pandas, requests, scipy, tabulate, yaml"

LABEL com.mappedsky.seizu.sandbox-protocol="seizu-sandbox/1"
WORKDIR /
EXPOSE 8080
ENTRYPOINT ["python3", "-I", "/opt/seizu-sandbox/server.py"]
