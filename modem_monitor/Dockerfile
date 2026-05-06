ARG BUILD_FROM=ghcr.io/hassio-addons/base:16.3.6
FROM ${BUILD_FROM}

WORKDIR /app

RUN apk add --no-cache python3 py3-pip

COPY modem.py /app/modem.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

RUN pip3 install --no-cache-dir --break-system-packages requests paho-mqtt urllib3
