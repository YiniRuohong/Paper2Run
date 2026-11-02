from __future__ import annotations

from pathlib import Path
from typing import Callable

from p2r_docparse.errors import DocParseError
from p2r_docparse.install import OfflineInstaller
from p2r_docparse.markers import ParseResult
from p2r_docparse.offline import parse_offline
from p2r_docparse.online import OnlineAgentClient, parse_online
from p2r_docparse.settings import DocParseSettings


class DocParseService:
    """Routes one parse to the mode locked at enqueue; never falls back between modes."""

    def __init__(self, root: Path, *, settings: DocParseSettings | None = None,
                 installer: OfflineInstaller | None = None,
                 online_client: OnlineAgentClient | None = None,
                 parse_online_fn=parse_online, parse_offline_fn=parse_offline) -> None:
        self.root = Path(root)
        self.settings = settings or DocParseSettings(self.root)
        self.installer = installer or OfflineInstaller(self.root)
        self.online_client = online_client
        self._parse_online = parse_online_fn
        self._parse_offline = parse_offline_fn

    def mode(self) -> str | None:
        return self.settings.mode()

    def parse(self, pdf: bytes, *, mode: str, previous_markdown: str | None = None,
              on_progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] = lambda: False) -> ParseResult:
        if mode == "offline":
            state = self.installer.state()
            if state.state != "ready" or not self.installer.ensure_healthy():
                raise DocParseError("DOCPARSE_OFFLINE_NOT_READY")
            return self._parse_offline(pdf, root=self.root, tier=state.tier, version=state.version,
                                       cancelled=cancelled)
        if mode == "online":
            client = self.online_client or OnlineAgentClient()
            return self._parse_online(pdf, client=client, previous_markdown=previous_markdown,
                                      on_progress=on_progress, cancelled=cancelled)
        raise DocParseError("DOCPARSE_MODE_UNSET")
