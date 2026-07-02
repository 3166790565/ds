FROM python:3.12-slim

WORKDIR /app

# 系统依赖（httpx 需要 SSL CA 证书）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 运行时数据目录（挂载 volume）
RUN mkdir -p data

# 暴露端口（实际端口映射由 docker-compose 控制）
EXPOSE 8080

# 默认启动命令（docker-compose 将 8080/9000 都映射到容器 8080）
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]