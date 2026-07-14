FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ /app/src/
COPY data/ /app/data/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.main"]
