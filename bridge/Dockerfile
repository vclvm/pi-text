ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY requirements.txt proxy.py ./
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" \
      --trusted-host "$(echo "$PIP_INDEX_URL" | sed -E 's#^[a-z]+://([^@/]*@)?([^/:]+).*#\2#')" -r requirements.txt
USER nobody
CMD ["sh", "-c", "exec uvicorn proxy:app --host 0.0.0.0 --port \"${BRIDGE_PORT:-8789}\""]
