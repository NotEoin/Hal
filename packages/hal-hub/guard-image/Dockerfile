# Guard worker image: Prompt Guard 2 on CPU, run with no network and read-only weights.
# 01-security "Taint, guard and reviewer", REQ-SEC-36. `hal guard install` builds it; the host
# relay (`python -m halhub.guard.serve`) runs it with the REQ-SEC-60 flags and the weights
# mounted read-only at /model. Base pinned by index digest (checked 2026-10-29).
FROM python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# --require-hashes: every wheel must match the lock; --no-deps: nothing outside it is installed
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY worker.py /opt/hal-guard/worker.py

# the relay passes the host uid with --user; this default keeps a bare `docker run` unprivileged
USER 65534:65534
ENTRYPOINT ["python", "-I", "/opt/hal-guard/worker.py"]
CMD ["--model-dir", "/model"]
