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

# SEM copiar o mapa do Notion para a imagem.
#
# Tentei antes: `COPY data/notion_map.json`. Quebrou o build com "failed to
# calculate checksum: /data/notion_map.json: not found", porque data/ esta no
# .gitignore - e esta certo, ali moram optmus.db e perfil.md.
#
# Commitar o arquivo tambem nao serve: ele carrega os database_id das bases
# pessoais e ESTE REPOSITORIO E PUBLICO. Nao sao credenciais (ler exige o
# token), mas sao identificadores estaveis de dado pessoal.
#
# Entao o mapa chega pelo ambiente, nao pela imagem: deixe-o em /data (volume
# da plataforma) ou aponte OPTMUS_NOTION_MAP_PATH. Sem ele, /notion/* e
# /relatorios/* respondem "mapa incompleto" - degradacao explicita, nao numero
# errado. Ver config/notion_map.example.json para o formato.

# Usuario sem privilegio: se uma ferramenta for enganada a executar algo, que
# seja com o menor poder possivel.
RUN useradd --create-home --uid 10001 optmus \
    && mkdir -p /data \
    && chown -R optmus:optmus /app /data
USER optmus

ENV OPTMUS_DATA_DIR=/data \
    OPTMUS_HTTP_HOST=0.0.0.0 \
    OPTMUS_HTTP_PORT=8420 \
    OPTMUS_ENV=prod \
    OPTMUS_VOICE_ENABLED=false

# SEM instrucao VOLUME aqui. A Railway recusa o build inteiro com
# "dockerfile invalid: docker VOLUME at Line N is not supported, use Railway
# Volumes" - la o disco persistente e recurso da plataforma, montado por fora.
#
# O efeito colateral e que /data so persiste se o volume estiver montado no
# painel: Settings -> Volumes -> mount path /data. Sem isso o container sobe
# igual, responde igual, e apaga TODA a memoria do Optmus a cada deploy - falha
# silenciosa, que so aparece quando ele esquece uma conversa de ontem.
#
# Para rodar a imagem fora da Railway, monte na mao:
#     docker run -v optmus-data:/data ...
# (era o que a instrucao VOLUME dava de graca, e agora e responsabilidade
# de quem executa.)

EXPOSE 8420

# Liveness usa a unica rota publica - o healthcheck nao deve carregar credencial.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url=f\"http://127.0.0.1:{os.environ.get('PORT', os.environ.get('OPTMUS_HTTP_PORT','8420'))}/health/live\"; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

# Shell form de proposito: precisa expandir $PORT, que a plataforma injeta.
#
# `python -m uvicorn`, nao `uvicorn`. As duas formas rodam o mesmo codigo, mas
# a primeira depende so do interpretador estar no PATH, e a segunda depende do
# diretorio de console scripts estar no PATH do shell que a plataforma usa para
# executar o start command. Custou um deploy: "/bin/bash: line 1: uvicorn:
# command not found" com o pacote instalado normalmente na imagem.
#
# SEM --log-config=/dev/null. Parecia inofensivo - "nao instale sua config de
# log" - mas o uvicorn manda o caminho para logging.config.fileConfig, que faz
# os.path.getsize e levanta RuntimeError("... is an empty file") para qualquer
# arquivo de tamanho zero. /dev/null tem tamanho zero no Linux, entao o
# processo morria no start, no container tanto quanto aqui. Omitir deixa o
# uvicorn com a config padrao dele, que nao conflita com o structlog: o
# configure_logging() do lifespan e quem manda no log da aplicacao.
CMD python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8420}
