FROM tcode-executor:py312

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends ncbi-blast+ \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT []
