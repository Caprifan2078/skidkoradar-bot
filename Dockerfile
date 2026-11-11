FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
RUN mkdir -p /app/data && useradd -r -u 10001 bot && chown -R bot:bot /app
USER bot
CMD ["python", "bot.py"]
