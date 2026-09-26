FROM python:3.11-slim

# 单容器应用：HTTP 服务 + SQLite 数据文件（/data 卷）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CASE_DB_PATH=/data/cases.db \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY service_09261_003 ./service_09261_003
COPY scripts ./scripts

RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8080

# slim 镜像没有 curl，用标准库做健康检查
HEALTHCHECK --interval=10s --timeout=3s --start-period=2s --retries=5 \
  CMD python3 -c "import json,os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=2).read()" || exit 1

CMD ["python3", "-m", "service_09261_003.api"]
