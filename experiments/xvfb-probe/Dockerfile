FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11-utils wget gnupg ca-certificates fonts-liberation \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcups2 \
      libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
      libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright

# bundled chromium always; real Google Chrome only exists for amd64
RUN playwright install chromium
RUN playwright install chrome || echo ">>> real Chrome unavailable on this arch"

COPY probe.py /probe.py
ENV DISPLAY=:99

CMD ["bash","-lc","Xvfb :99 -screen 0 1920x1080x24 -ac >/dev/null 2>&1 & sleep 3; xdpyinfo -display :99 >/dev/null && echo 'Xvfb up' ; python /probe.py"]
