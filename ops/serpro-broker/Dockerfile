FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SERPRO_ENVIRONMENT=fake
WORKDIR /broker
COPY broker.py .
COPY serpro_broker/ serpro_broker/
CMD ["python", "broker.py"]
