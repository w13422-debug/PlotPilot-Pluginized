"""Public, dependency-light PlotPilot plugin SDK for contract v1.

The SDK is intentionally the single implementation used by P0 fixtures and
future plugin projects.  It exposes deterministic hashing, strict contract
validation, framed RPC, and typed ports without granting plugins Core DB
access or direct publication authority.
"""

from .canonical import canonical_bytes, hash_jcs, parse_json_bytes, sha256_hex
from .errors import ContractError, ContractValidationError, ErrorCode
from .package import (
    PackageDigest,
    build_files_sha256,
    normalize_relative_path,
    package_hash,
    release_id,
    skill_package_hash,
    skill_release_id,
)
from .verifier import (
    assert_valid,
    load_strict_json,
    validate_contract,
    validate_rpc_response,
    validate_rpc_request,
    verify_backup,
    verify_checkpoint,
    verify_core_snapshot,
    verify_data_bundle,
    verify_manifest,
    verify_package_identity,
    verify_package_manifest,
    verify_plan,
    verify_result_bundle,
    verify_restore_report,
    verify_skill_chain,
    verify_skill_identity,
    verify_skill_receipt,
    verify_sse_recovery,
    verify_snapshot,
    verify_stream_prefix,
)
from .rpc import ChunkUploadLedger, OperationLedger
from .fixtures import HttpSSEFixture, PluginUIHostFixture

__all__ = [
    "ContractError",
    "ContractValidationError",
    "ErrorCode",
    "PackageDigest",
    "assert_valid",
    "build_files_sha256",
    "canonical_bytes",
    "ChunkUploadLedger",
    "HttpSSEFixture",
    "hash_jcs",
    "load_strict_json",
    "OperationLedger",
    "PluginUIHostFixture",
    "normalize_relative_path",
    "package_hash",
    "parse_json_bytes",
    "release_id",
    "sha256_hex",
    "skill_package_hash",
    "skill_release_id",
    "validate_contract",
    "validate_rpc_response",
    "validate_rpc_request",
    "verify_backup",
    "verify_checkpoint",
    "verify_core_snapshot",
    "verify_data_bundle",
    "verify_manifest",
    "verify_package_identity",
    "verify_package_manifest",
    "verify_plan",
    "verify_result_bundle",
    "verify_restore_report",
    "verify_skill_chain",
    "verify_skill_identity",
    "verify_skill_receipt",
    "verify_sse_recovery",
    "verify_snapshot",
    "verify_stream_prefix",
]
