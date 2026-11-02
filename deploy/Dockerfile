FROM python:3.11.9-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 MLOPS_DATA_DIR=/app/runtime
WORKDIR /app
COPY requirements-lock.txt ./
RUN pip install -r requirements-lock.txt \
    && useradd --create-home --uid 10001 mlops \
    && mkdir /app/runtime && chown mlops:mlops /app/runtime
COPY src ./src
USER mlops
EXPOSE 8765
CMD ["python", "-m", "src", "--host", "0.0.0.0"]
