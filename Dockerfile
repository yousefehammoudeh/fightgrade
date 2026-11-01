# The Dagster+ Serverless image. A Docker build (not the fast PEX deploy)
# because the crawlers (Tapology, UFC Stats, MMADecisions) need system packages: Google Chrome, its
# libraries, and Xvfb for the virtual display headed Chrome draws into.
#
# The project is installed editable from /app, so the code sees the repo's own
# layout — scrapers/ for the extractor, dbt/ for the models — exactly as on a
# laptop.
FROM python:3.13-slim-bookworm

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb curl ca-certificates zstd \
 && rm -rf /var/lib/apt/lists/*

# Ollama, for the news classifier (pipeline/news_classify.py): the release the
# backfill ran on, CPU only (a Serverless run has 4 vCPUs and no GPU), so the GPU
# backends' directories are dropped. The model's weights (~2.5 GB) are not baked
# in: Fargate caches no image layers, so every run of every job would pull them.
# news_classify pulls them at its start and checks their digest.
ARG TARGETARCH=amd64
ARG OLLAMA_VERSION=0.40.13
RUN curl -fsSL "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-${TARGETARCH}.tar.zst" \
    | tar --zstd -x -C /usr \
 && find /usr/lib/ollama -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

WORKDIR /app
COPY . /app
RUN pip install -e /app

# Chrome plus every system library it needs, installed by the Playwright that
# drives it, so the two versions always match.
RUN python -m playwright install --with-deps chrome

RUN dagster-cloud --version && Xvfb -help >/dev/null 2>&1 && ollama --version \
 && python -c "import pipeline.tapology, pipeline.ufcstats, pipeline.mmadecisions, pipeline.news_classify"
