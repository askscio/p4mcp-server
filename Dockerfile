FROM python:3.12-slim

# Install system dependencies and p4 CLI
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    && wget -q https://cdist2.perforce.com/perforce/r24.2/bin.linux26x86_64/p4 -O /usr/local/bin/p4 \
    && chmod +x /usr/local/bin/p4 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Set environment variables
ENV PYTHONPATH=/app

# Run the server with HTTP transport
CMD ["python3", "-m", "src.main", "--transport", "stdio"]