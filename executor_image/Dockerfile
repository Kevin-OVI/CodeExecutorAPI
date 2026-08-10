FROM python:3.14.6-trixie@sha256:7655aadf4ac71023baa42d7e1430a61d2ca80798af2ffb71c3533baafe68695b

ARG NODESOURCE_COMMIT=9b431d8ae0f10df272598585855c6eca6c0e1bd2
ARG NODESOURCE_SHA256=6e3d580f5bd7ccf2aa1e8df8d35c60d78e873c3ff8beb282c9bebd914904ad72

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  curl -fsSL "https://raw.githubusercontent.com/nodesource/distributions/${NODESOURCE_COMMIT}/scripts/deb/setup_24.x" -o /tmp/nodesource_setup.sh && \
  echo "${NODESOURCE_SHA256}  /tmp/nodesource_setup.sh" | sha256sum -c - && \
  bash /tmp/nodesource_setup.sh && rm /tmp/nodesource_setup.sh && \
  apt update && apt upgrade -y && apt dist-upgrade -y && apt full-upgrade -y && \
  apt install man aptitude curl wget git tree ca-certificates gnupg lsb-release net-tools nmap telnet dnsutils cmatrix fortune cowsay lolcat iputils-ping nodejs gcc openjdk-25-jdk-headless -y && \
  apt clean

RUN --mount=type=cache,target=/root/.cache/pip \
  pip install -U pip==26.2.1 && \
  pip install -U numpy==2.5.2 scipy==1.18.0 requests==2.34.2 matplotlib==3.11.1 graphviz==0.21 networkx==3.6.1 pandas==3.0.5 beautifulsoup4==4.15.0 pillow==12.3.0 sympy==1.14.0 seaborn==0.13.2 plotly==6.9.0 bokeh==3.9.2 faker==40.36.0 websockets==17.0.1 aiohttp==3.14.3 yt-dlp==2026.7.4 imageio==2.37.4 cryptography==50.0.0 emoji==2.15.0 more-itertools==11.1.0 tabulate==0.10.0 humanize==4.16.0 boltons==26.1.0 python-dateutil==2.9.0.post0 python-whois==0.9.6 && \
  pip cache purge

COPY executors/* /executors/
RUN chmod +x /executors/*.sh

RUN useradd appuser -ms /sbin/nologin -d /app
USER appuser

WORKDIR /app
