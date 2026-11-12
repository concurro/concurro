# The Concurro backoffice API: FastAPI on uvicorn, reading tasks with each signed-in user's GitHub
# token and run diagnostics from the service's diagnostics directory. Built from the host's clone,
# like the service, because it reuses the engine's and service's decoders.
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d
COPY service/requirements.txt /tmp/service-requirements.txt
COPY backoffice/requirements.txt /tmp/backoffice-requirements.txt
RUN pip install --no-cache-dir -r /tmp/service-requirements.txt -r /tmp/backoffice-requirements.txt \
    && rm /tmp/*-requirements.txt
COPY engine /opt/concurro/engine
COPY bundle /opt/concurro/bundle
COPY service /opt/concurro/service
COPY backoffice /opt/concurro/backoffice
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/concurro
USER 65533:65533
ENTRYPOINT ["python3", "-m", "backoffice", "--settings", "/etc/concurro/backoffice/settings.json"]
CMD ["--listen", "0.0.0.0:8788"]
