FROM mcr.microsoft.com/playwright:v1.56.1-noble

USER root
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

RUN mkdir -p /opt/tall/browser \
    && cd /opt/tall/browser \
    && npm init -y \
    && npm install playwright@1.56.1

ENV NODE_PATH=/opt/tall/browser/node_modules
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

USER pwuser
WORKDIR /workspace
ENV HOME=/tmp/home

ENTRYPOINT []
