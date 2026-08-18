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
    && pip install ".[llm,memoria,relatorios]"

COPY core/ ./core/
COPY memory/ ./memory/
COPY tools/ ./tools/
COPY security/ ./security/
COPY expression/ ./expression/
COPY perception/ ./perception/
# integrations/ e reports/ NAO estavam aqui, e o main.py importa os dois. O
# container subia com ModuleNotFoundError e a "correcao" foi comentar os
# imports, ate sobrar um main.py de 16 linhas. Nao remova estas duas linhas
# sem remover os imports correspondentes.
COPY integrations/ ./integrations/
COPY reports/ ./reports/
COPY main.py ./

# O mapa das bases do Notion e configuracao, nao dado gerado: sem ele todo
# /notion/* responde "mapa incompleto". Fica na imagem, e nao em /data, porque
# /data e volume - um COPY para la seria mascarado pela montagem no primeiro
# deploy. O caminho e apontado por OPTMUS_NOTION_MAP_PATH mais abaixo.
COPY data/notion_map.json ./config/notion_map.json

# Usuario sem privilegio: se uma ferramenta for enganada a executar algo, que
# seja com o menor poder possivel.
RUN useradd --create-home --uid 10001 optmus \
    && mkdir -p /data \
    && chown -R optmus:optmus /app /data
USER optmus

# Volume persistente. SEM isto, o SQLite vive no layer efemero do container e
# TODA a memoria do Optmus e apagada a cada deploy.
ENV OPTMUS_DATA_DIR=/data \
    OPTMUS_NOTION_MAP_PATH=/app/config/notion_map.json \
    OPTMUS_HTTP_HOST=0.0.0.0 \
    OPTMUS_HTTP_PORT=8420 \
    OPTMUS_ENV=prod \
    OPTMUS_VOICE_ENABLED=false

# Sem volume, o SQLite vive no layer efemero e TODA a memoria do Optmus e
# apagada a cada deploy. Na Railway, monte um volume em /data pelo painel -
# esta linha declara a intencao e vale para docker run local.
VOLUME ["/data"]

EXPOSE 8420

# Liveness usa a unica rota publica - o healthcheck nao deve carregar credencial.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url=f\"http://127.0.0.1:{os.environ.get('PORT', os.environ.get('OPTMUS_HTTP_PORT','8420'))}/health/live\"; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

# Shell form de proposito: precisa expandir $PORT, que a plataforma injeta.
#
# SEM --log-config=/dev/null. Parecia inofensivo - "nao instale sua config de
# log" - mas o uvicorn manda o caminho para logging.config.fileConfig, que faz
# os.path.getsize e levanta RuntimeError("... is an empty file") para qualquer
# arquivo de tamanho zero. /dev/null tem tamanho zero no Linux, entao o
# processo morria no start, no container tanto quanto aqui. Omitir deixa o
# uvicorn com a config padrao dele, que nao conflita com o structlog: o
# configure_logging() do lifespan e quem manda no log da aplicacao.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8420}
