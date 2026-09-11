FROM python:alpine3.20

LABEL version="2024-08-22"
LABEL description="Downloads and converts audiobooks \
from audible to m4b and saves them in the audiobooks directory"

WORKDIR /app

ENV AUDIBLE_CONFIG_DIR=/config
ENV WEB_PORT=8080

RUN mkdir -p /audiobooks /config /app

RUN apk update \
	&& apk add --update --no-cache ffmpeg

RUN pip install --no-cache-dir audible-cli==0.6.0 Flask

RUN apk del gcc musl-dev python3-dev

COPY app/ /app/

RUN chown -R 1000:999 /app /audiobooks /config
USER 1000:999

EXPOSE 8080

CMD ["python", "/app/web.py"]
