"""Resolve the persistent instance root for scripts shipped in the ops bundle."""

import os
from pathlib import Path


class MissingInstanceRoot(ValueError):
    pass


def instance_root(script: str) -> Path:
    configured = os.environ.get("CLIPROXY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(script).resolve().parent.parent
    if ".fleet-releases" in root.parts:
        raise MissingInstanceRoot("CLIPROXY_ROOT is required when running from the ops bundle")
    return root
