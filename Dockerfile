FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# GDAL provides the DXF driver. LibreDWG supplies dwg2dxf for DWG uploads.
# Prefer the distro package when available, and build the utility otherwise
# because libredwg-utils is not present in every Debian slim repository.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        build-essential \
        autoconf \
        automake \
        libtool \
        pkg-config \
        wget \
        gdal-bin \
        libgdal-dev \
    && if apt-cache show libredwg-utils >/dev/null 2>&1; then \
        apt-get install --no-install-recommends -y libredwg-utils; \
    else \
        wget -q https://ftp.gnu.org/gnu/libredwg/libredwg-0.13.3.tar.xz \
        && tar -xf libredwg-0.13.3.tar.xz \
        && cd libredwg-0.13.3 \
        && ./configure --prefix=/usr/local \
        && make -j"$(nproc)" \
        && make install \
        && ldconfig \
        && cd /app \
        && rm -rf /app/libredwg-0.13.3 /app/libredwg-0.13.3.tar.xz; \
    fi \
    && command -v dwg2dxf \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]