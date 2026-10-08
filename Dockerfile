FROM python:3.14-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS build
ENV UV_NO_CACHE=1 UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=2 UV_CONCURRENT_BUILDS=1 UV_CONCURRENT_INSTALLS=1
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.2
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM build AS test
RUN apk add --no-cache bash
RUN uv sync --frozen --no-editable
COPY tests ./tests
COPY scripts ./scripts
COPY deploy ./deploy
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["/app/.venv/bin/pytest", "-q", "-p", "no:cacheprovider"]

FROM python:3.14-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS runtime
RUN python -m pip uninstall --yes pip \
    && addgroup -g 10001 bot && adduser -D -H -u 10001 -G bot bot \
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
