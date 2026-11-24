# Updater: stessa base Python dei bot + client Docker/Compose (binari statici dall'immagine ufficiale)
FROM docker:27-cli AS dockercli
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY update.sh /update.sh
RUN chmod +x /update.sh
ENTRYPOINT ["/update.sh"]
