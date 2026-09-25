FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
# Only the headless shell: the server never shows a browser.
RUN .venv/bin/playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/*
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && mkdir /data && chown app /data
USER app
# Dependencies are resolved at build time; start the venv's scripts directly.
ENV PATH=/app/.venv/bin:$PATH ICAMPUS_DATA_DIR=/data ICAMPUS_HOST=0.0.0.0
EXPOSE 9013 8724
CMD ["icampus", "serve"]
