FROM python:3.14.7-trixie

RUN (curl -fsSL https://deb.nodesource.com/setup_24.x | bash) && \
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
  wget https://packages.microsoft.com/config/debian/13/packages-microsoft-prod.deb -O packages-microsoft-prod.deb && \
  dpkg -i packages-microsoft-prod.deb && rm packages-microsoft-prod.deb && \
  apt update && apt upgrade -y && apt dist-upgrade -y && apt full-upgrade -y && \
  apt install ca-certificates cmatrix cowsay curl dnsutils dotnet-sdk-8.0 fortune g++ gcc \
    git gnupg iputils-ping lolcat lsb-release man net-tools nmap nodejs \
    openjdk-25-jdk-headless telnet tree wget -y && \
  apt clean

ENV PATH="/root/.cargo/bin:${PATH}"

RUN pip install -U pip && \
  pip install -U aiohttp beautifulsoup4 bokeh boltons cryptography dateparser emoji \
    faker fpdf graphviz humanize imageio loguru markdown matplotlib more-itertools \
    networkx numpy pandas pillow plotly pydub python-dateutil python-whois requests \
    scipy seaborn sympy tabulate watchdog websockets yt-dlp && \
  pip cache purge

COPY executors/* /executors/
RUN chmod +x /executors/*.sh

RUN useradd appuser -ms /sbin/nologin -d /app
USER appuser

WORKDIR /app
