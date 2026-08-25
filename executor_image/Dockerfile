FROM python:3.14.7-trixie

RUN (curl -fsSL https://deb.nodesource.com/setup_24.x | bash) && \
  apt update && apt upgrade -y && apt dist-upgrade -y && apt full-upgrade -y && \
  apt install man aptitude curl wget git tree ca-certificates gnupg lsb-release net-tools nmap telnet dnsutils cmatrix fortune cowsay lolcat iputils-ping nodejs gcc openjdk-25-jdk-headless -y && \
  apt clean

RUN pip install -U pip && \
  pip install -U numpy scipy requests matplotlib graphviz networkx pandas beautifulsoup4 pillow sympy seaborn plotly bokeh faker websockets aiohttp yt-dlp imageio cryptography emoji more-itertools tabulate humanize boltons python-dateutil python-whois && \
  pip cache purge

COPY executors/* /executors/
RUN chmod +x /executors/*.sh

RUN useradd appuser -ms /sbin/nologin -d /app
USER appuser

WORKDIR /app
