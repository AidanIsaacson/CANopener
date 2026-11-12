# CANopener upload server: one standard-library Python file, nothing to pip install.
FROM python:3.12-slim

RUN useradd --system --uid 10001 --home-dir /data canopener \
    && mkdir -p /data && chown canopener /data
COPY server.py /app/server.py

USER canopener
ENV DATA_DIR=/data HOST=0.0.0.0 PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"
CMD ["python", "/app/server.py"]
