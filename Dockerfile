FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS build
ENV UV_NO_CACHE=1 UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=2 UV_CONCURRENT_BUILDS=1 UV_CONCURRENT_INSTALLS=1
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.2
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM build AS test
RUN uv sync --frozen --no-editable
COPY tests ./tests
COPY scripts ./scripts
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["/app/.venv/bin/pytest", "-q", "-p", "no:cacheprovider"]

FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS runtime
RUN python -m pip uninstall --yes pip \
    && groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /data && chown bot:bot /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL="sqlite+aiosqlite:////data/app.sqlite3"
USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python", "-m", "nutrition_bot", "healthcheck"]
ENTRYPOINT ["python", "-m", "nutrition_bot"]
CMD ["run"]
