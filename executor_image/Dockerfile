# syntax=docker/dockerfile:1
FROM python:3.14.7-trixie

RUN rm -f /etc/apt/apt.conf.d/docker-clean && \
  echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,id=apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,id=apt-lists,sharing=locked \
  apt update && apt upgrade -y && \
  apt install -y curl wget

ENV RUSTUP_HOME=/usr/local/rustup \
  CARGO_HOME=/usr/local/cargo

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
  chmod -R a+rX "$RUSTUP_HOME" "$CARGO_HOME"

RUN wget https://packages.microsoft.com/config/debian/13/packages-microsoft-prod.deb -O packages-microsoft-prod.deb && \
  dpkg -i packages-microsoft-prod.deb && rm packages-microsoft-prod.deb

RUN --mount=type=cache,target=/var/cache/apt,id=apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,id=apt-lists,sharing=locked \
  (curl -fsSL https://deb.nodesource.com/setup_24.x | bash) && \
  apt update && \
  apt install -y dotnet-sdk-10.0 nodejs openjdk-25-jdk-headless

RUN --mount=type=cache,target=/var/cache/apt,id=apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,id=apt-lists,sharing=locked \
  apt install -y ca-certificates cmatrix cowsay dnsutils fortune g++ gcc \
    git gnupg iputils-ping lolcat lsb-release man net-tools nmap telnet tree

RUN --mount=type=cache,target=/root/.cache/pip,id=pip,sharing=locked \
  pip install -U pip && \
  pip install -U aiohttp beautifulsoup4 bokeh boltons cryptography dateparser emoji \
    faker fpdf graphviz humanize imageio loguru markdown matplotlib more-itertools \
    networkx numpy pandas pillow plotly pydub python-dateutil python-whois requests \
    scipy seaborn sympy tabulate watchdog websockets yt-dlp

ENV PATH="${CARGO_HOME}/bin:/usr/games:${PATH}"
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  DOTNET_NOLOGO=1 \
  DOTNET_SKIP_WORKLOAD_INTEGRITY_CHECK=true \
  DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=true

COPY executors/* /executors/
RUN chmod +x /executors/*.sh

RUN useradd appuser -ms /sbin/nologin -d /app
USER appuser

WORKDIR /app
