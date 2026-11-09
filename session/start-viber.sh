#!/bin/bash
set -e

export DISPLAY=:99
# Chromium inside Qt WebEngine can't use its sandbox in an unprivileged container
export QTWEBENGINE_DISABLE_SANDBOX=1
export QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"
# No GPU in the container: render OpenGL in software (mesa llvmpipe)
export LIBGL_ALWAYS_SOFTWARE=1

# Wait for the virtual screen
until [ -e /tmp/.X11-unix/X99 ]; do sleep 0.5; done

# A crashed previous run leaves single-instance locks behind
rm -f /tmp/qtsingleapp-Viber* /tmp/qtsingleapp*

exec dbus-run-session -- /opt/viber/Viber
