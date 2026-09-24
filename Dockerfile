FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION LICENSE NOTICE ./
COPY licenses/ licenses/
COPY successfactors_toolkit/ successfactors_toolkit/
RUN mkdir -p /data/tenants && chown -R appuser:appgroup /data
# A fresh named volume copies this directory's mode, so any --user UID:GID
# can create its own PII_VAULT_DIR subdirectory under it (mode 0700, set by
# the app itself) without a host bind mount or root-owned mount point.
RUN mkdir -m 1777 /vault
USER appuser
EXPOSE 8000
CMD ["uvicorn", "successfactors_toolkit.main:app", "--host", "0.0.0.0", "--port", "8000"]
