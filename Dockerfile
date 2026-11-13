FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOKEN_FILE=/data/tokens.json

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY volvo_mqtt ./volvo_mqtt

USER app
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "volvo_mqtt"]
CMD ["run"]
