# Use official lightweight Python image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

# Install system dependencies required for scientific libraries and C/C++ extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies first for optimal Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application codebase
COPY . .

# Create placeholders for database and output directories
RUN mkdir -p /app/database /app/webapp/output

# Expose the Flask port
EXPOSE 5000

# Start with Gunicorn production WSGI server
# --workers 1    : single process so in-memory session state is shared
# --threads 4    : handle concurrent requests (polling, uploads, etc.)
# --timeout 300  : 5-min timeout for long pipeline/optimization requests
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "300", "--preload", "webapp.app:app"]
