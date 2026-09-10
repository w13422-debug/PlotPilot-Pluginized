from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKEND = str(ROOT / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from plotpilot_core.api.v1.core import CoreHttpAdapter
from plotpilot_core.assets import AssetStore
from plotpilot_core.bootstrap.production_plugin_runtime import (
    build_production_plugin_runtime,
)
from plotpilot_core.bootstrap.webui_core_runtime import (
    WebUiCoreRuntime,
)
from plotpilot_core.publication import PublicationService
from plotpilot_core.repositories.authority import (
    CoreAuthorityRepository,
)
from plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)
from plotpilot_core.supervisor import IsolatedVenvProcessFactory


def test_p8_webui_and_plugin_runtime_share_every_authority_object(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "library"
    core_root = data_root / "core"
    core_root.mkdir(parents=True)
    repository = CoreAuthorityRepository(core_root / "core.db")
    assets = AssetStore(data_root / "assets")
    publication = PublicationService(repository, assets)
    core_authority = CoreAuthorityApplication(repository, publication)
    adapter = CoreHttpAdapter(core_authority, publication, assets)
    webui = WebUiCoreRuntime(
        repository=repository,
        assets=assets,
        publication_service=publication,
        authority=core_authority,
        adapter=adapter,
    )
    try:
        runtime = build_production_plugin_runtime(webui)
        assert runtime.data_root == data_root.resolve()
        with repository.read_connection() as connection:
            assert runtime.lifecycle.connection is connection
        assert runtime.core_runtime is webui
        assert runtime.repository is webui.repository is repository
        assert runtime.assets is webui.assets is assets
        assert runtime.lifecycle.core_authority_binding is repository
        assert runtime.execution.repository is repository
        assert runtime.execution.assets is assets
        assert runtime.retirement.repository is runtime.lifecycle
        assert runtime.routes.lifecycle is runtime.lifecycle
        assert runtime.routes.packages is runtime.packages
        assert runtime.routes.retirement is runtime.retirement
        assert runtime.authority.repository is repository
        assert runtime.authority.lifecycle is runtime.lifecycle
        assert runtime.authority.retirement is runtime.retirement
        assert runtime.authority.execution is runtime.execution
        assert runtime.authority.routes is runtime.routes
        assert runtime.authority.route_authority is runtime.routes
        assert runtime.lookup._routes is runtime.routes
        assert runtime.lookup._packages is runtime.packages
        assert runtime.lookup._authority is runtime.authority
        assert runtime.supervisor._authority is runtime.authority
        assert runtime.supervisor._lookup is runtime.lookup
        assert runtime.supervisor._processes is runtime.processes
        assert isinstance(runtime.processes, IsolatedVenvProcessFactory)
        assert runtime.supervisor.status("com.plotpilot.autopilot") is None
    finally:
        webui.close()
