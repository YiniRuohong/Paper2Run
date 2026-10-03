"""Provider extraction backed by runtime-only provider sessions."""
from __future__ import annotations

from .extraction_service import ExtractionService
from .provider_sessions import ProviderSecretStore, ProviderSessionClosedError
from .providers import (
    AsyncTransport,
    Capabilities,
    OpenAICompatibleProvider,
    ProviderError,
)


class SessionExtractionService:
    """Resolve a live provider session and delegate validated extraction."""

    def __init__(
        self,
        store: ProviderSecretStore,
        *,
        transport: AsyncTransport | None = None,
        max_context_bytes: int = 128 * 1024,
    ) -> None:
        self._store = store
        self._transport = transport
        self._max_context_bytes = max_context_bytes

    async def extract(
        self,
        *,
        paper_id: str,
        paper_sha256: str,
        content: bytes,
        provider_config_id: str,
        model_id: str,
        call_id: str,
        prompt_version: str,
        policy_version: str,
    ) -> dict[str, object]:
        session_id = self._store.latest_session_id(provider_config_id, model_id)
        if session_id is None:
            raise ProviderError("auth_invalid")
        try:
            config = self._store.config(session_id).model_copy(update={
                "capabilities": Capabilities(
                    text="supported",
                    structured_output="supported",
                ),
            })
        except ProviderSessionClosedError:
            raise ProviderError("auth_invalid") from None

        def resolve_secret() -> str:
            try:
                return self._store.get(session_id)
            except ProviderSessionClosedError:
                raise ProviderError("auth_invalid") from None

        provider = OpenAICompatibleProvider(
            config,
            transport=self._transport,
            secret_resolver=resolve_secret,
        )
        return await ExtractionService(
            provider,
            max_context_bytes=self._max_context_bytes,
        ).extract(
            paper_id=paper_id,
            paper_sha256=paper_sha256,
            content=content,
            provider_config_id=provider_config_id,
            model_id=model_id,
            call_id=call_id,
            prompt_version=prompt_version,
            policy_version=policy_version,
        )
