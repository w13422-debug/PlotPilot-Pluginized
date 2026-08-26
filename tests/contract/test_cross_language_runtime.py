from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_typescript_verifier  # noqa: E402


VERIFIER = ROOT / "frontend" / "src" / "contracts" / "verifier.ts"


def _run_ts(script: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update({"NODE_NO_WARNINGS": "1", "PLOTPILOT_TS_VERIFIER": str(VERIFIER)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, f"node verifier failed\nstdout={completed.stdout}\nstderr={completed.stderr}"
    return json.loads(completed.stdout)


def test_real_typescript_verifier_positive_runtime() -> None:
    result = verify_typescript_verifier()
    assert result == {"collision_rejected": True, "status": "ok", "workspace_id": "ws-1"}


def test_typescript_verifier_negative_runtime_matches_python_contract() -> None:
    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const verifier = await import(pathToFileURL(process.env.PLOTPILOT_TS_VERIFIER).href)
const root = process.cwd()
const readJson = path => JSON.parse(readFileSync(`${root}/${path}`, 'utf8'))
const snapshot = readJson('contracts/golden/run-snapshot/snapshot.json')
const bundle = readJson('contracts/examples/result-bundle.json')
const data = readJson('contracts/examples/fixtures/plugin-data-bundle.json')
const heartbeat = readJson('contracts/examples/fixtures/rpc-notification.json')

const rejected = async action => {
  try {
    await action()
    return false
  } catch (_) {
    return true
  }
}

await verifier.verifySnapshot(snapshot)
verifier.verifyResultProfile(bundle, snapshot.workspace_id)
await verifier.verifyDataBundle(data)

const observed = {
  nfc_casefold_equal: verifier.unicodeNfcCasefold('cafe\u0301.txt') === verifier.unicodeNfcCasefold('caf\u00e9.txt'),
  sharp_s_casefold_equal: verifier.unicodeNfcCasefold('straße.txt') === verifier.unicodeNfcCasefold('strasse.txt'),
  data_hash_tamper_rejected: await rejected(async () => {
    const bad = { ...data, bundle_hash: '0'.repeat(64) }
    await verifier.verifyDataBundle(bad)
  }),
  workspace_mismatch_rejected: await rejected(() => verifier.verifyResultProfile(bundle, 'workspace-other')),
  duplicate_result_id_rejected: await rejected(() => verifier.verifyResultProfile({ ...bundle, items: [bundle.items[0], { ...bundle.items[0] }] }, snapshot.workspace_id)),
  heartbeat_stale_rejected: await rejected(() => verifier.validateRpcRequest(heartbeat, 2)),
  missing_checkpoint_rejected: await rejected(() => verifier.validateRpcResult('job.pause', { accepted: true, checkpoint_asset_id: null })),
  capability_ref_rejected: await rejected(() => verifier.verifyCapabilityDescriptor(readJson('contracts/examples/fixtures/capability-provider.json'), 'unknown.capability/v1')),
}

if (!Object.values(observed).every(Boolean)) throw new Error(`unexpected runtime observations: ${JSON.stringify(observed)}`)
console.log(JSON.stringify(observed))
'''
    observed = _run_ts(script)
    assert observed == {
        "nfc_casefold_equal": True,
        "sharp_s_casefold_equal": True,
        "data_hash_tamper_rejected": True,
        "workspace_mismatch_rejected": True,
        "duplicate_result_id_rejected": True,
        "heartbeat_stale_rejected": True,
        "missing_checkpoint_rejected": True,
        "capability_ref_rejected": True,
    }
