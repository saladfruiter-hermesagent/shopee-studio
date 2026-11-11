FROM python:3.13-slim
ARG APP_VERSION=local
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data TZ=America/Sao_Paulo APP_VERSION=$APP_VERSION
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 10001 studio && useradd -u 10001 -g studio -m studio \
    && mkdir /data && chown studio:studio /data
COPY --chown=studio:studio app.py core.py content.py analysis.py llm.py video.py modern.py studio.py ./
COPY --chown=studio:studio static/ static/
COPY --chown=studio:studio templates/ templates/
USER studio
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"
CMD ["python", "app.py"]
