# Alpine 环境一致、镜像更小；如需 Debian 基础可改用 python:3.11-slim
FROM python:3.11-alpine

WORKDIR /app

# 纯标准库脚本，无需 pip install
COPY monitor.py /app/monitor.py

# 以 root 运行，才能写入挂载进来的宿主机 /etc/hosts
# 如需降权可自行调整，但写 hosts 必须有写权限
CMD ["python", "/app/monitor.py"]
