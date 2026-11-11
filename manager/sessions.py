#!/usr/bin/env python3
"""Session lifecycle: provision / refresh / destroy Viber containers.

One container + one volume per session, named by the session id. The manager never publishes
container ports; it drives everything by name via docker_ops.
"""

import uuid

from . import docker_ops, link


class SessionManager:
    def __init__(self, registry, image, memory, log):
        self.reg = registry
        self.image = image
        self.memory = memory
        self.log = log

    def create(self):
        sid = uuid.uuid4().hex[:10]
        container = f"viber-session-{sid}"
        volume = f"viber-{sid}"
        ok, err = docker_ops.run_container(container, self.image, volume, self.memory)
        if not ok:
            raise RuntimeError(f"docker run failed: {err}")
        row = self.reg.create(sid, container, volume)
        self.log(f"[session] created {sid} -> {container}")
        return row

    def refresh(self, sid):
        """Re-probe link status until linked, then freeze it."""
        row = self.reg.get(sid)
        if not row:
            return None
        if row["status"] != "linked":
            status, number = link.probe(row["container"])
            if status != row["status"] or number != row["number"]:
                row = self.reg.update(sid, status=status, number=number)
        return row

    def upgrade_outdated(self):
        """Recreates session containers that run an older build of the session image. The Viber
        login and the reader's cursor live on the volume, so the session stays linked; the inbound
        worker reattaches to the new container's log by name. Run before the workers start."""
        current = docker_ops.image_id(self.image)
        if not current:
            raise RuntimeError(f"session image {self.image} not found")
        for row in self.reg.all():
            if docker_ops.container_image_id(row["container"]) in (None, current):
                continue
            # Viber ignores SIGTERM and has no other clean-quit path, so this is a hard kill (as is
            # any docker stop). SQLite usually survives it; once it didn't (Viber's "Something went
            # wrong"), and "Fix now" there wipes the profile, which the reaper reports as unlinked.
            docker_ops.stop_and_remove(row["container"])
            ok, err = docker_ops.run_container(row["container"], self.image, row["volume"], self.memory)
            if not ok:
                raise RuntimeError(f"recreating {row['container']} failed: {err}")
            self.log(f"[session] {row['id']} recreated on the current image")

    def delete(self, sid):
        row = self.reg.get(sid)
        if not row:
            return False
        docker_ops.stop_and_remove(row["container"])
        docker_ops.remove_volume(row["volume"])
        self.reg.delete(sid)
        self.log(f"[session] deleted {sid}")
        return True
