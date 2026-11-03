ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY requirements.txt proxy.py ./
# Your company's CA certificates, if any (pibox setup puts them into ca/): trusted by pip while
# building and by the bridge when it calls the router.
COPY requirements.txt c[a] /tmp/ca/
RUN set -eu; for c in /tmp/ca/*.crt; do [ -e "$c" ] && cp "$c" /usr/local/share/ca-certificates/; done; \
    update-ca-certificates >/dev/null 2>&1 || true; rm -rf /tmp/ca
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" \
      --trusted-host "$(echo "$PIP_INDEX_URL" | sed -E 's#^[a-z]+://([^@/]*@)?([^/:]+).*#\2#')" -r requirements.txt
USER nobody
CMD ["sh", "-c", "exec uvicorn proxy:app --host 0.0.0.0 --port \"${BRIDGE_PORT:-8789}\""]
