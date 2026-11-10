FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements-api.txt requirements-dev.txt requirements-frontend.txt ./
RUN pip install -r requirements-api.txt && pip install pytest==9.2.0
COPY app ./app
COPY config ./config
COPY eval ./eval
COPY tests ./tests
COPY db/__init__.py ./db/__init__.py
COPY db/seed ./db/seed
COPY scripts ./scripts
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
