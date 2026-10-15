FROM python:3.11-slim

# weasyprint (PDF report generation) needs Pango/Cairo system libs;
# libpq-dev needed to build psycopg2 wheels on some platforms.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf2.0-0 \
    libffi-dev shared-mime-info libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

# Release step, then the app (ARCH-01): migrations run once per container
# start, before the server accepts traffic. For several replicas, run
# `python scripts/migrate.py` as a separate one-off release job instead and
# start replicas with `python app.py`.
CMD ["sh", "-c", "python scripts/migrate.py && exec python app.py"]
