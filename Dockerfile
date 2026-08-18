# Optmus Core - imagem do "cerebro na nuvem".
#
# Esta imagem sobe o Core SEM voz e SEM dispositivos: um container nao tem
# microfone, nao tem placa de som e nao tem os celulares da sua mesa no USB.
# O que roda aqui e o que faz sentido remoto - API, memoria, ferramentas,
# consolidador noturno. A camada de voz continua na sua maquina e fala com
# este processo por HTTP.
#
# Python 3.12: faster-whisper, openwakeword e mediapipe param nessa minor.
# Mesmo sem instalar o extra [voz] aqui, manter a mesma minor evita que a
# imagem e a maquina local divirjam em comportamento.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias primeiro: o pyproject muda menos que o codigo, entao esta camada
# fica em cache entre builds.
COPY pyproject.toml README.md ./
COPY core/__init__.py ./core/
RUN pip install --upgrade pip \
    && pip install ".[llm,memoria]"

COPY core/ ./core/
COPY memory/ ./memory/
COPY tools/ ./tools/
COPY security/ ./security/
COPY expression/ ./expression/
COPY perception/ ./perception/
COPY main.py ./

# Usuario sem privilegio: se uma ferramenta for enganada a executar algo, que
# seja com o menor poder possivel.
RUN useradd --create-home --uid 10001 optmus \
    && mkdir -p /data \
    && chown -R optmus:optmus /app /data
USER optmus

# Volume persistente. SEM isto, o SQLite vive no layer efemero do container e
# TODA a memoria do Optmus e apagada a cada deploy.
ENV OPTMUS_DATA_DIR=/data \
    OPTMUS_HTTP_HOST=0.0.0.0 \
    OPTMUS_HTTP_PORT=8420 \
    OPTMUS_ENV=prod \
    OPTMUS_VOICE_ENABLED=false
VOLUME ["/data"]

EXPOSE 8420

# Liveness usa a unica rota publica - o healthcheck nao deve carregar credencial.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url=f\"http://127.0.0.1:{os.environ.get('PORT', os.environ.get('OPTMUS_HTTP_PORT','8420'))}/health/live\"; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

# Shell form de proposito: precisa expandir $PORT, que a plataforma injeta.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8420} --log-config=/dev/null
