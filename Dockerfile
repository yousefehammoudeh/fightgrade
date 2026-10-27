# The Dagster+ Serverless image. A Docker build (not the fast PEX deploy)
# because the Tapology and UFC Stats crawlers need system packages: Google Chrome, its
# libraries, and Xvfb for the virtual display headed Chrome draws into.
#
# The project is installed editable from /app, so the code sees the repo's own
# layout — scrapers/ for the extractor, dbt/ for the models — exactly as on a
# laptop.
FROM python:3.13-slim-bookworm

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install -e /app

# Chrome plus every system library it needs, installed by the Playwright that
# drives it, so the two versions always match.
RUN python -m playwright install --with-deps chrome

RUN dagster-cloud --version && Xvfb -help >/dev/null 2>&1 && python -c "import pipeline.tapology, pipeline.ufcstats"
