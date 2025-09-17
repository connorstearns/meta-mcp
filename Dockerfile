# Python slim is small and fast to build
FROM python:3.11-slim

# Make logs unbuffered and avoid .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Cloud Run sends traffic to $PORT; listen on 0.0.0.0
# Use shell form so ${PORT} expands (fallback to 8080 locally)
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
