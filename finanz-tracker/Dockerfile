ARG BUILD_FROM=alpine:3.18
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-flask

WORKDIR /app

COPY app.py .
COPY templates/ templates/
COPY static/ static/
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 5000

CMD ["/run.sh"]
