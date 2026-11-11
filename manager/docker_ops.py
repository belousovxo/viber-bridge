#!/usr/bin/env python3
"""Thin wrappers over the `docker` CLI used by the manager.

The manager talks to each session's container by name — never by published ports — so it works
the same whether the manager runs on the host or as a container with the docker socket mounted.
"""

import subprocess


def _run(args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, **kw)


SESSION_NETWORK = "viber-sessions"


def ensure_session_network():
    """Session containers get internet but must not reach each other (or be reached from other
    containers): their screen (noVNC) is unauthenticated. A bridge network with inter-container
    traffic disabled does both."""
    if _run(["network", "inspect", SESSION_NETWORK]).returncode != 0:
        r = _run(["network", "create", "-o", "com.docker.network.bridge.enable_icc=false", SESSION_NETWORK],
                 text=True)
        if r.returncode != 0:
            raise RuntimeError(f"docker network create {SESSION_NETWORK} failed: {r.stderr.strip()}")


def run_container(name, image, volume, memory, shm_size="1g"):
    """docker run -d a session container with an isolated home volume on the session network. It
    comes back on its own after a crash or host reboot (the profile on the volume keeps it logged
    in). Returns (ok, stderr)."""
    args = ["run", "-d", "--name", name, "--restart", "unless-stopped", "--memory", memory,
            "--shm-size", shm_size, "--network", SESSION_NETWORK, "-v", f"{volume}:/home/viber", image]
    r = _run(args, text=True)
    return r.returncode == 0, (r.stderr or "").strip()


def image_id(image):
    r = _run(["image", "inspect", "-f", "{{.Id}}", image], text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def container_image_id(name):
    r = _run(["inspect", "-f", "{{.Image}}", name], text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def container_running(name):
    r = _run(["inspect", "-f", "{{.State.Running}}", name], text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def stop_and_remove(name):
    _run(["rm", "-f", name], text=True)


def remove_volume(volume):
    _run(["volume", "rm", "-f", volume], text=True)


def exec_sh(name, script, binary=False, timeout=30):
    """Run `sh -c <script>` inside the container. Returns (ok, stdout) — bytes if binary."""
    r = _run(["exec", name, "sh", "-c", script], text=not binary, timeout=timeout)
    out = r.stdout if r.stdout is not None else (b"" if binary else "")
    return r.returncode == 0, out


def exec_argv(name, argv, timeout=120, env=None):
    """Run an explicit argv inside the container (no shell). Returns the CompletedProcess.

    `env` injects `-e KEY=VALUE` flags — needed for UI automation that must join the container's
    running session (DISPLAY, the shared AT-SPI/D-Bus session bus), which `docker exec` does not
    inherit on its own.
    """
    flags = []
    for k, v in (env or {}).items():
        flags += ["-e", f"{k}={v}"]
    return _run(["exec", *flags, name, *argv], text=True, timeout=timeout)


def cp_in(name, src_path, dst_path):
    """docker cp a local file/dir into the container. Returns True on success."""
    r = _run(["cp", src_path, f"{name}:{dst_path}"], text=True)
    return r.returncode == 0


def cp_out(name, src_path, dst_path):
    """docker cp a file out of the container. Returns True on success."""
    r = _run(["cp", f"{name}:{src_path}", dst_path], text=True)
    return r.returncode == 0


def logs_follow(name, tail="0"):
    """Return a Popen streaming `docker logs -f`. Caller reads .stdout and terminates it."""
    return subprocess.Popen(
        ["docker", "logs", "-f", "--tail", tail, name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
