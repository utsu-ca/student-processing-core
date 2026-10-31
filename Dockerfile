# Portal image: runs on Cloud Run, Azure Container Apps, Fly, Kubernetes, or plain Docker. Pick the database at run time
# with PORTAL_STORE (see wiki/Deployment.md); drivers for all supported backends are installed.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[portal,firestore,sql]" "psycopg[binary]>=3" pymysql
RUN useradd --system --no-create-home app
USER app
ENV PORT=8080
CMD ["sh", "-c", "exec uvicorn --factory utsu_portal:create_app --host 0.0.0.0 --port ${PORT} --no-server-header --proxy-headers --forwarded-allow-ips='*'"]
