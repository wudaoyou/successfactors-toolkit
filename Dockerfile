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
USER appuser
EXPOSE 8000
CMD ["uvicorn", "successfactors_toolkit.main:app", "--host", "0.0.0.0", "--port", "8000"]
