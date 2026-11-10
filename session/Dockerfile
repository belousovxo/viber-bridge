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
      # real ALSA: otherwise apt satisfies Viber's dep with the OSS shim (liboss4-salsa), missing symbols Viber needs
      libasound2t64 \
      fonts-noto-core fonts-noto-color-emoji \
      sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Viber Desktop — pinned build. We vendor the exact .deb (see session/README) instead of
# pulling "latest" from the CDN, so a rebuild never silently changes the Viber version.
# Expected: viber_29.4.0.7_amd64.deb
COPY vendor/viber.deb /tmp/viber.deb
RUN apt-get update \
    && apt-get install -y --no-install-recommends /tmp/viber.deb \
    && rm -rf /tmp/viber.deb /var/lib/apt/lists/*

# python3 for the reader/agent, xdotool+xclip to drive sending via the UI, inotify-tools for the
# reader's change trigger, and gcc/g++/Qt6 to build the key-capture shim and the DB-read harness
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      xdotool xclip inotify-tools \
      gcc g++ libc6-dev pkg-config qt6-base-dev \
    && rm -rf /var/lib/apt/lists/*

# Build the SQLCipher key-capture shim (memcpy interposition, see hexkey_hook.c)
COPY hexkey_hook.c /tmp/hexkey_hook.c
RUN gcc -shared -fPIC -O2 -o /usr/local/lib/hexkey_hook.so /tmp/hexkey_hook.c -ldl && rm -f /tmp/hexkey_hook.c

# Build the Qt harness that reads viber.db through Viber's own sqlite plugin (see viber_dump.cpp).
# Compiled against system Qt6; at runtime it loads Viber's Qt libs + plugin via LD_LIBRARY_PATH.
COPY viber_dump.cpp /tmp/viber_dump.cpp
RUN g++ -O2 -fPIC /tmp/viber_dump.cpp -o /usr/local/bin/viber_dump \
      $(pkg-config --cflags --libs Qt6Sql Qt6Core) && rm -f /tmp/viber_dump.cpp

# Open noVNC at the site root instead of /vnc.html.
# X socket dir must exist up front: Xvfb runs as a non-root user and can't create it.
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# AT-SPI accessibility stack — lets the sender wait for the target chat to actually render
# (event-driven, no guessed delays) and verify the right chat opened. Trailing layer so the
# Qt toolchain layer above stays cached.
RUN apt-get update && apt-get install -y --no-install-recommends \
      at-spi2-core python3-pyatspi python3-gi \
    && rm -rf /var/lib/apt/lists/*

# ubuntu:24.04 ships user "ubuntu" with uid 1000; rename it to "viber"
RUN usermod -l viber -d /home/viber -m ubuntu && groupmod -n viber ubuntu

COPY supervisord.conf /etc/supervisor/viber-session.conf
COPY start-viber.sh /usr/local/bin/start-viber.sh
COPY decrypt_plugin.py /usr/local/bin/decrypt_plugin.py
COPY reader.py /usr/local/bin/reader.py
COPY send.py /usr/local/bin/send.py
COPY agent.py /usr/local/bin/agent.py
RUN chmod +x /usr/local/bin/start-viber.sh

USER viber
WORKDIR /home/viber

# Viber profile (session, local DB) lives in /home/viber/.ViberPC — keep /home/viber in a volume
VOLUME ["/home/viber"]

EXPOSE 6080
CMD ["supervisord", "-c", "/etc/supervisor/viber-session.conf"]
