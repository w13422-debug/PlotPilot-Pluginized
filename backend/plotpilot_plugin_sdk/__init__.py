"""Public, dependency-light PlotPilot plugin SDK for contract v1."""

from __future__ import annotations

import importlib
import sys
from importlib.machinery import PathFinder

_CANONICAL_NAME = "backend.plotpilot_plugin_sdk"
_PUBLIC_NAME = "plotpilot_plugin_sdk"
_BACKEND_NAMESPACE = "backend"


def _canonical_package_spec_exists() -> bool:
    if _BACKEND_NAMESPACE in sys.modules:
        backend_path = getattr(sys.modules[_BACKEND_NAMESPACE], "__path__", None)
    else:
        backend_spec = PathFinder.find_spec(_BACKEND_NAMESPACE)
        backend_path = (
            None if backend_spec is None else backend_spec.submodule_search_locations
        )
    return (
        backend_path is not None
        and PathFinder.find_spec(_CANONICAL_NAME, backend_path) is not None
    )


_initialize_current_package = True
# Inspect package specs without executing an unrelated backend package.  If the
# canonical package exists, all errors from its real import must propagate.
if __name__ == _PUBLIC_NAME and _canonical_package_spec_exists():
    sys.modules[_PUBLIC_NAME] = importlib.import_module(_CANONICAL_NAME)
    _initialize_current_package = False

if _initialize_current_package:
    # Publish the alias before importing SDK members so every subsequent top-level
    # import observes this exact package object.
    sys.modules[_PUBLIC_NAME] = sys.modules[__name__]

    from .canonical import canonical_bytes, hash_jcs, parse_json_bytes, sha256_hex
    from .context_identity import (
        derive_operation_context_identity,
        operation_context_projection,
    )
    from .errors import ContractError, ContractValidationError, ErrorCode
    from .fixtures import HttpSSEFixture, PluginUIHostFixture
    from .package import (
        PackageDigest,
        build_files_sha256,
        normalize_relative_path,
        package_hash,
        release_id,
        skill_package_hash,
        skill_release_id,
    )
    from .rpc import ChunkUploadLedger, OperationLedger
    from .verifier import (
        assert_valid,
        load_strict_json,
        validate_contract,
        validate_rpc_request,
        validate_rpc_response,
        verify_backup,
        verify_checkpoint,
        verify_core_snapshot,
        verify_data_bundle,
        verify_manifest,
        verify_package_identity,
        verify_package_manifest,
        verify_plan,
        verify_restore_report,
        verify_result_bundle,
        verify_skill_chain,
        verify_skill_identity,
        verify_skill_receipt,
        verify_snapshot,
        verify_sse_recovery,
        verify_stream_prefix,
    )

    for _name, _module in tuple(sys.modules.items()):
        if _name.startswith(_CANONICAL_NAME + "."):
            sys.modules[_PUBLIC_NAME + _name[len(_CANONICAL_NAME):]] = _module

    __all__ = [
        "ChunkUploadLedger",
        "ContractError",
        "ContractValidationError",
        "ErrorCode",
        "HttpSSEFixture",
        "OperationLedger",
        "PackageDigest",
        "PluginUIHostFixture",
        "assert_valid",
        "build_files_sha256",
        "canonical_bytes",
        "derive_operation_context_identity",
        "hash_jcs",
        "load_strict_json",
        "normalize_relative_path",
        "operation_context_projection",
        "package_hash",
        "parse_json_bytes",
        "release_id",
        "sha256_hex",
        "skill_package_hash",
        "skill_release_id",
        "validate_contract",
        "validate_rpc_request",
        "validate_rpc_response",
        "verify_backup",
        "verify_checkpoint",
        "verify_core_snapshot",
        "verify_data_bundle",
        "verify_manifest",
        "verify_package_identity",
        "verify_package_manifest",
        "verify_plan",
        "verify_restore_report",
        "verify_result_bundle",
        "verify_skill_chain",
        "verify_skill_identity",
        "verify_skill_receipt",
        "verify_snapshot",
        "verify_sse_recovery",
        "verify_stream_prefix",
    ]
