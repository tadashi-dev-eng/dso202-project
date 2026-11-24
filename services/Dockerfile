# services/Dockerfile -- TaskFlow (DSO202 final project)
# One Dockerfile builds all 14 Python services:
#
#   docker build -f services/Dockerfile --build-arg SERVICE=task-service \
#       -t localhost:5001/taskflow/task-service:0.1.0 services/
#
# Layers are ordered from most shared to least shared. The pip layer and
# the common-library layer are byte-identical for every service, so the
# registry and each kind node store them once; only the last COPY differs.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv/common:/srv

# Fixed non-root UID: the Pod securityContext sets runAsNonRoot and the
# same UID, and the root filesystem is mounted read-only.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY common/ /srv/common/

ARG SERVICE
ARG VERSION=dev
ENV SERVICE_NAME=${SERVICE} \
    SERVICE_VERSION=${VERSION}
COPY ${SERVICE}/app/ /srv/app/

USER 10001
EXPOSE 8000
# --proxy-headers: X-Forwarded-* from the gateway/ingress are honoured.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
