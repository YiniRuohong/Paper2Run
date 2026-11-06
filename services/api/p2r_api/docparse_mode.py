"""The PDF parse mode the product uses.

Only local (offline MinerU) parsing is offered, and it is the default: there is
no choice to make. Online parsing (uploading the paper to mineru.net) is switched
off, so a setting saved as "online" earlier is ignored. On a platform that cannot
run the offline component there is no mode at all (None).
"""
from __future__ import annotations


def local_mode(service) -> str | None:
    return None if service.installer.state().state == "unsupported" else "offline"
