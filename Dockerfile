FROM python:3.11-slim
ARG PANDOC_VERSION=3.1.11
# OCR for scanned PDFs. Extra languages: --build-arg OCR_LANG_PACKAGES="tesseract-ocr-eng tesseract-ocr-ara" (and set OCR_LANGS=eng+ara)
ARG OCR_LANG_PACKAGES="tesseract-ocr-eng"
# Recent pandoc (native OMML equations, docx writer); Debian's apt version is too old to rely on.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && ARCH=$(dpkg --print-architecture) \
 && curl -fsSL -o /tmp/pandoc.deb "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-${ARCH}.deb" \
 && apt-get install -y /tmp/pandoc.deb && rm -rf /tmp/pandoc.deb /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr ${OCR_LANG_PACKAGES} \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["sh", "-c", "alembic upgrade head && python -m bot.main"]
