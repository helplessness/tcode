ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS python-base

USER root

# 替换为国内镜像源，解决国内网络无法访问 deb.debian.org 的问题
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources

# primer3-py does not publish a Linux ARM64 wheel. Build one for the current
# target architecture, then copy only the wheel into the runtime image. This
# keeps gcc/make/Cython out of the final sandbox image and also works with
# buildx multi-platform builds.
FROM python-base AS primer3-builder

ARG PRIMER3_PY_VERSION=2.3.0
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential \
       make \
    && python -m pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" \
       setuptools \
       wheel \
       Cython \
    && python -m pip wheel --no-cache-dir --no-build-isolation --no-deps \
       --index-url "${PYPI_INDEX_URL}" \
       --wheel-dir /opt/primer3-wheels \
       "primer3-py==${PRIMER3_PY_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

FROM python-base

ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash \
       ca-certificates \
       curl \
       git \
       nodejs \
       npm \
       pdftk-java \
       poppler-utils \
       procps \
       qpdf \
       ripgrep \
       tesseract-ocr \
       tesseract-ocr-chi-sim \
       tesseract-ocr-eng \
       util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=primer3-builder /opt/primer3-wheels /opt/primer3-wheels

RUN python -m pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" \
    pdf2image \
    pdfplumber \
    PyMuPDF \
    pypdf \
    pytesseract \
    faiss-cpu \
    reportlab \
    pandas \
    openpyxl \
    /opt/primer3-wheels/primer3_py-*.whl \
    && python -c "import openpyxl, primer3; assert primer3.__version__ == '2.3.0'" \
    && rm -rf /opt/primer3-wheels

RUN useradd -m -u 10000 runner

USER runner
WORKDIR /workspace
ENV HOME=/tmp/home

ENTRYPOINT []
