FROM python:3.11-slim

WORKDIR /app

# tesseract: OCR opcional para PDFs escaneados
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=7860
EXPOSE 7860

# gthread permite atender /progreso mientras se está convirtiendo.
# Render inyecta PORT=10000; Hugging Face usa 7860 por defecto.
CMD gunicorn --worker-class gthread --workers 1 --threads 8 --timeout 0 --bind 0.0.0.0:${PORT:-7860} app:app
