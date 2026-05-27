FROM python:3.11-slim

WORKDIR /workspace

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates sqlite3 && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p /workspace/data /workspace/config

EXPOSE 10308

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10308"]
