FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN useradd --system --uid 1000 --create-home jellylook \
    && mkdir -p /app/data \
    && chown -R jellylook:jellylook /app/data
USER jellylook
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
