FROM python:3.13-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY exporter.py /app/exporter.py
ENV STATE_FILE=/state/ions-state.json \
    PYTHONUNBUFFERED=1
VOLUME ["/state"]
EXPOSE 9596
CMD ["python3", "/app/exporter.py"]
