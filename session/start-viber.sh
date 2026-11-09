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

# Capture the SQLCipher key Viber sets at startup (see hexkey_hook.c). Set CAPTURE_KEY=0 to disable.
if [ "${CAPTURE_KEY:-1}" = "1" ] && [ -f /usr/local/lib/hexkey_hook.so ]; then
    export VIBER_KEY_OUT="${VIBER_KEY_OUT:-/tmp/viber-key.hex}"
    export LD_PRELOAD="/usr/local/lib/hexkey_hook.so"
    : > "$VIBER_KEY_OUT" 2>/dev/null || true
fi

exec dbus-run-session -- /opt/viber/Viber
