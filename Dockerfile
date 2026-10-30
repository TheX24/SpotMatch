FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
COPY web/requirements.txt web/
RUN pip install --no-cache-dir -r web/requirements.txt

COPY spotmatch_core.py ./
COPY web/server.py web/
COPY web/static web/static

USER nobody
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"
CMD ["python", "-m", "uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log", "--limit-concurrency", "300", "--timeout-keep-alive", "5"]
