# CD Service — Python FastAPI
FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    fastapi uvicorn paramiko pymysql bcrypt requests pydantic-settings

COPY . .

EXPOSE 8081

CMD ["python", "main.py"]
