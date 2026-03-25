# poc/services/sms-adapter/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 공통 base 모듈 복사
COPY services/base /app/base

# SMS Adapter 소스 복사
COPY services/sms-adapter /app/sms-adapter

# 의존성 설치 (base의 requirements.txt 사용)
RUN pip install --no-cache-dir -r /app/base/requirements.txt

WORKDIR /app/sms-adapter

ENV ADAPTER_PORT=8101
ENV LOG_LEVEL=INFO

EXPOSE 8101

CMD ["python", "main.py"]