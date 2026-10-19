# Base image with Python and Playwright pre-installed
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Install ffmpeg for media merging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser dependencies
RUN playwright install chromium

# Copy application files
COPY . /app/

# Default entry point
CMD ["python", "pad.py", "--check"]
