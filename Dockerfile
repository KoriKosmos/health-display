FROM lscr.io/linuxserver/baseimage-selkies:debiantrixie

ENV TITLE="Health Display"

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 curl ca-certificates && \
    curl -fL -o /tmp/discord.deb "https://discord.com/api/download?platform=linux&format=deb" && \
    apt-get install -y /tmp/discord.deb && \
    rm -rf /tmp/discord.deb /var/lib/apt/lists/*

COPY health_display.py /usr/local/bin/health-display
COPY root/ /
RUN chmod 755 /usr/local/bin/health-display /defaults/discord-session
