FROM ghcr.io/astral-sh/uv:0.9.21 AS uv
FROM python:3.15-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY bizzy ./bizzy
COPY manage.py LICENSE ./
RUN groupadd --gid 10001 bizzy && useradd --uid 10001 --gid bizzy --no-create-home bizzy && install -d -o bizzy -g bizzy /data
USER 10001:10001
EXPOSE 8080
CMD ["/app/.venv/bin/python", "manage.py", "serve"]
