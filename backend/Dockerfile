FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
COPY run.py .

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && python run.py"]
