# Imagen única de web y worker (el compose la comparte; cambia el command).
# git: workspace.py maneja worktrees. docker CLI estático: el worker lanza el
# runner contra el docker.sock del host (Docker-outside-of-Docker); la web no
# tiene el sock montado, así que el binario ahí es inerte.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gettext \
    && rm -rf /var/lib/apt/lists/*

ARG DOCKER_CLI_VERSION=27.3.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# UI bilingüe: compilar las traducciones (.po → .mo) en el build.
RUN python manage.py compilemessages
