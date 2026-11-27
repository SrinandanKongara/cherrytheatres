FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.56.0
COPY worker.py .
CMD ["python", "-u", "worker.py"]
