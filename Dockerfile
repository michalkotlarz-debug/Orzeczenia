FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Warsaw
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN useradd -r -u 1000 orzecznik && chown -R orzecznik:orzecznik /app
USER orzecznik
EXPOSE 8000
CMD ["python", "-m", "orzeczenia.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
