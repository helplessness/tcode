FROM python:3.12-slim

# 插件代码和 SDK 在运行时只读挂载；镜像只提供固定 Python 解释器和非 root 身份。
RUN groupadd --gid 65532 tcode-plugin \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /tmp tcode-plugin \
    && mkdir -p /tcode/runtime/tcode_agent \
    && printf '%s\n' '"""TCode Plugin SDK container runtime."""' > /tcode/runtime/tcode_agent/__init__.py

COPY --chown=65532:65532 tcode_agent/plugin_sdk /tcode/runtime/tcode_agent/plugin_sdk

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/tcode/runtime

USER 65532:65532
WORKDIR /tmp

CMD ["python3", "-m", "tcode_agent.plugin_sdk.worker"]
