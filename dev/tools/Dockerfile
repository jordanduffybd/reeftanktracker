FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mirror_from_prod.py .

# Unbuffered stdout/stderr so docker-compose logs are live.
ENV PYTHONUNBUFFERED=1

CMD ["python", "mirror_from_prod.py"]
