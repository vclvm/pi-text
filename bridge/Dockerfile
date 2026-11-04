ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY requirements.txt ./
# Your company's CA certificates, if any (pibox setup puts them into ca/): trusted by pip while
# building and by the bridge when it calls the router.
COPY requirements.txt c[a] /tmp/ca/
RUN set -eu; for c in /tmp/ca/*.crt; do [ -e "$c" ] && cp "$c" /usr/local/share/ca-certificates/; done; \
    update-ca-certificates >/dev/null 2>&1 || true; rm -rf /tmp/ca
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
# Your pip.conf, if pibox setup put one into build/ (repository.pip_conf): pip reads your index
# and login from it itself. Mounted for this step only, so the login doesn't stay in the image.
# pip's downloads stay in BuildKit's cache between builds.
RUN --mount=type=bind,source=.,target=/ctx --mount=type=cache,target=/root/.cache/pip \
    if [ -f /ctx/build/pip.conf ]; then export PIP_CONFIG_FILE=/ctx/build/pip.conf; fi; \
    pip install -r requirements.txt
# The code last: changing it doesn't reinstall the dependencies.
COPY proxy.py probe.py ./
USER nobody
CMD ["sh", "-c", "exec uvicorn proxy:app --host 0.0.0.0 --port \"${BRIDGE_PORT:-8789}\""]
