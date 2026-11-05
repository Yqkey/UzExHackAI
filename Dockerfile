FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# База собирается заранее (data/exchange.duckdb лежит в репозитории, ~20 МБ), иначе: python build_db.py /путь/к/csv
ENV PORT=8000
CMD ["sh","-c","uvicorn web:app --host 0.0.0.0 --port ${PORT}"]
