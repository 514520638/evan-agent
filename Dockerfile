FROM ghcr.io/astral-sh/uv:0.9.25 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project && uv cache clean
RUN useradd --uid 10001 --create-home agent && mkdir -p /app/.local && chown agent:agent /app/.local
COPY src ./src
COPY evals ./evals
# 保持 editable 安装，让项目的数据默认路径稳定为 /app/.local。
RUN uv sync --frozen --no-dev && uv cache clean
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
EXPOSE 8001
CMD ["uvicorn", "evan_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
