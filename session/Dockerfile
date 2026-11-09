# One Viber Desktop session per container:
# Xvfb (virtual screen) + fluxbox (window manager) + x11vnc + noVNC (screen in the browser) + Viber.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Europe/Kyiv

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      xvfb x11vnc fluxbox novnc websockify supervisor \
      dbus dbus-x11 \
      libgl1-mesa-dri \
      fonts-noto-core fonts-noto-color-emoji \
      sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Viber Desktop (official .deb). apt resolves its declared dependencies.
RUN curl -fsSL -o /tmp/viber.deb https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/viber.deb \
    && rm -rf /tmp/viber.deb /var/lib/apt/lists/*

# Open noVNC at the site root instead of /vnc.html
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# ubuntu:24.04 ships user "ubuntu" with uid 1000; rename it to "viber"
RUN usermod -l viber -d /home/viber -m ubuntu && groupmod -n viber ubuntu

COPY supervisord.conf /etc/supervisor/viber-session.conf
COPY start-viber.sh /usr/local/bin/start-viber.sh
RUN chmod +x /usr/local/bin/start-viber.sh

USER viber
WORKDIR /home/viber

# Viber profile (session, local DB) lives in /home/viber/.ViberPC — keep /home/viber in a volume
VOLUME ["/home/viber"]

EXPOSE 6080
CMD ["supervisord", "-c", "/etc/supervisor/viber-session.conf"]
