# ── Build Stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# 빌드 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime Stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 런타임 의존성: ffmpeg(오디오) + libsodium(암호화)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsodium23 \
    && rm -rf /var/lib/apt/lists/*

# 빌드 스테이지에서 패키지 복사
COPY --from=builder /install /usr/local

# 소스 코드 복사 (모든 .py 파일은 저장소 루트에 위치)
COPY *.py ./

# 환경 설정
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

# 헬스체크: Keep-Alive 서버가 응답하는지 확인
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["python", "bot.py"]
