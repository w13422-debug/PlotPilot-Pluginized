"""Real WebUI composition for the frozen Core v1 HTTP surface."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from fastapi import FastAPI

from application import paths
from backend.plotpilot_core.api.v1.core import CoreHttpAdapter, create_core_router
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)


@dataclass(slots=True)
class WebUiCoreRuntime:
    """Own the one Core v1 authority graph attached to a WebUI application."""

    repository: CoreAuthorityRepository
    assets: AssetStore
    publication_service: PublicationService
    authority: CoreAuthorityApplication
    adapter: CoreHttpAdapter
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def closed(self) -> bool:
        """Whether the authority repository has been closed for this app."""

        with self._close_lock:
            return self._closed

    def close(self) -> None:
        """Close the single Core repository once, including repeated shutdowns."""

        with self._close_lock:
            if self._closed:
                return
            self.repository.close()
            self._closed = True


def build_webui_core_runtime() -> WebUiCoreRuntime:
    """Compose the Core v1 production graph from the canonical data root."""

    data_dir = Path(paths.DATA_DIR)
    core_dir = data_dir / "core"
    core_dir.mkdir(parents=True, exist_ok=True)

    repository = CoreAuthorityRepository(core_dir / "core.db")
    assets = AssetStore(data_dir / "assets")
    publication_service = PublicationService(repository, assets)
    authority = CoreAuthorityApplication(repository, publication_service)
    adapter = CoreHttpAdapter(authority, publication_service, assets)
    return WebUiCoreRuntime(
        repository=repository,
        assets=assets,
        publication_service=publication_service,
        authority=authority,
        adapter=adapter,
    )


def mount_webui_core_runtime(app: FastAPI) -> WebUiCoreRuntime:
    """Attach one real Core v1 router to ``app`` and retain its lifecycle owner."""

    if getattr(app.state, "webui_runtime", None) is not None:
        raise RuntimeError("WebUI Core runtime is already mounted")

    runtime = build_webui_core_runtime()
    try:
        app.include_router(create_core_router(runtime.adapter))
    except BaseException:
        runtime.close()
        raise
    app.state.webui_runtime = runtime
    return runtime


__all__ = [
    "WebUiCoreRuntime",
    "build_webui_core_runtime",
    "mount_webui_core_runtime",
]
