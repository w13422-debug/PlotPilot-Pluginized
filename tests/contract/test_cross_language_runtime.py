from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_typescript_verifier  # noqa: E402


VERIFIER = ROOT / "frontend" / "src" / "contracts" / "verifier.ts"


def _run_ts(script: str, extra_environment: dict[str, str] | None = None) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update({"NODE_NO_WARNINGS": "1", "PLOTPILOT_TS_VERIFIER": str(VERIFIER)})
    if extra_environment:
        environment.update(extra_environment)
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


def test_typescript_v2_public_surface_matches_python_semantics() -> None:
    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const core = await import(pathToFileURL('frontend/src/contracts/core-api-v2.ts').href)
const http = await import(pathToFileURL('frontend/src/contracts/m4-m5-http-v2.ts').href)
const root = 'contracts/golden/m4-m5-public-surface-v2/'
const read = name => JSON.parse(readFileSync(root + name, 'utf8'))
const candidate = read('candidate.json')
const publication = read('publication.json')
const job = read('job.json')
const story = read('story-state.json')
const httpGoldens = read('http.json')
const candidates = new Map([candidate.candidate, candidate.candidate_partial, candidate.candidate_incomplete_stream].map(item => [item.candidate_id, item]))
const rejected = async action => { try { await action(); return false } catch (_) { return true } }

core.parseCandidateV2(candidate.candidate)
core.validatePublicationV2(publication.command_complete, publication.result_complete, candidate.candidate, 'ws-1')
core.parseStoryStateProjectionInputV2(story.projection, 'ws-1')
await core.verifyJobSnapshotHashV2(job.snapshot)
await http.parseJobSseRecoveryV2(job.sse_replay)
for (const exchange of httpGoldens.exchanges) await http.validateHttpExchangeV2(exchange.route_id, exchange.request, exchange.status, exchange.response, candidates.get(exchange.request.candidate_id))
const crossWorkspace = structuredClone(candidate.candidate)
crossWorkspace.target.workspace_id = 'ws-other'
const cursorMix = structuredClone(job.event_page)
cursorMix.next_cursor = 'candidate/ws-1/1'
const partial = structuredClone(publication.command_partial)
const partialResult = structuredClone(publication.result_complete)
partialResult.publication_operation_key = partial.publication_operation_key
partialResult.candidate_id = partial.candidate_id
partialResult.content_hash = candidate.candidate_partial.mutation.payload_hash
const observed = {
  cross_workspace_rejected: await rejected(() => core.parseCandidateV2(crossWorkspace)),
  partial_publication_rejected: await rejected(() => core.validatePublicationV2(partial, partialResult, candidate.candidate_partial, 'ws-1')),
  cursor_mix_rejected: await rejected(() => core.parseJobEventPageV2(cursorMix)),
  sse_query_pair_rejected: await rejected(() => http.parseHttpRequestV2('job.sse-recovery', { ...httpGoldens.exchanges.find(item => item.route_id === 'job.sse-recovery').request, after_seq: 0 })),
  plugin_lifecycle_guard_rejected: await rejected(() => http.parseHttpRequestV2('plugin.install', { ...httpGoldens.exchanges.find(item => item.route_id === 'plugin.install').request, release_id: null })),
  exchange_binding_rejected: await rejected(() => http.validateHttpExchangeV2('publication.accept', httpGoldens.exchanges.find(item => item.route_id === 'publication.accept').request, 200, { ...httpGoldens.exchanges.find(item => item.route_id === 'publication.accept').response, content_hash: '0'.repeat(64) }, candidates.get('candidate-v2'))),
}
if (!Object.values(observed).every(Boolean)) throw new Error(`unexpected v2 observations: ${JSON.stringify(observed)}`)
console.log(JSON.stringify(observed))
'''
    assert _run_ts(script) == {
    "cross_workspace_rejected": True,
    "cursor_mix_rejected": True,
    "exchange_binding_rejected": True,
    "plugin_lifecycle_guard_rejected": True,
    "partial_publication_rejected": True,
    "sse_query_pair_rejected": True,
  }


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


def test_typescript_consumes_frozen_casefold_table_for_all_generated_mappings(tmp_path: Path) -> None:
    contract = json.loads((ROOT / "contracts" / "unicode-casefold-v1.json").read_text(encoding="utf-8"))
    # Compare the real runtime implementations on every generated mapping
    # source.  Expected values come from the Python SDK, including its frozen
    # NFC step, rather than from a second JS normalization implementation.
    from plotpilot_plugin_sdk.verifier import unicode_nfc_casefold

    samples = [
        {"input": chr(int(codepoint, 16)), "expected": unicode_nfc_casefold(chr(int(codepoint, 16)))}
        for codepoint in contract["mappings"]
    ]
    samples_path = tmp_path / 'casefold-samples.json'
    samples_path.write_text(json.dumps(samples, ensure_ascii=True), encoding='utf-8')
    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const verifier = await import(pathToFileURL(process.env.PLOTPILOT_TS_VERIFIER).href)
const contract = JSON.parse(readFileSync('contracts/unicode-casefold-v1.json', 'utf8'))
const mappings = Object.entries(contract.mappings)
const samples = JSON.parse(readFileSync(process.env.PLOTPILOT_CASEFOLD_SAMPLES_PATH, 'utf8'))
const mappingParity = samples.every(sample => verifier.unicodeNfcCasefold(sample.input) === sample.expected)
const rejected = async action => {
  try {
    await action()
    return false
  } catch (_) {
    return true
  }
}

const a7cb = String.fromCodePoint(0xA7CB) + '.txt'
const openE = String.fromCodePoint(0x264) + '.txt'
await verifier.buildFilesSha256({ [a7cb]: new Uint8Array([1]), [openE]: new Uint8Array([2]) })
const observed = {
  mapping_count: mappings.length,
  mapping_parity: mappingParity,
    fixture_vectors: contract.test_vectors.every(vector => verifier.unicodeNfcCasefold(vector.input) === vector.expected),
    nfc_composition_vectors: contract.nfc_test_vectors.every(vector => verifier.unicodeNfc(vector.input) === vector.expected),
  a7cb_distinct_from_open_e: verifier.unicodeNfcCasefold(a7cb) !== verifier.unicodeNfcCasefold(openE),
  nfc_vector: verifier.unicodeNfcCasefold('cafe\u0301.txt') === 'café.txt',
  sharp_s_collision_rejected: await rejected(() => verifier.buildFilesSha256({ 'Straße.txt': new Uint8Array([1]), 'strasse.txt': new Uint8Array([2]) })),
}
  if (!observed.mapping_parity || !observed.fixture_vectors || !observed.nfc_composition_vectors || !observed.a7cb_distinct_from_open_e || !observed.nfc_vector || !observed.sharp_s_collision_rejected) {
  throw new Error(`unexpected frozen casefold observations: ${JSON.stringify(observed)}`)
}
console.log(JSON.stringify(observed))
'''
    assert _run_ts(script, {"PLOTPILOT_CASEFOLD_SAMPLES_PATH": str(samples_path)}) == {
        "mapping_count": 1530,
        "mapping_parity": True,
        "fixture_vectors": True,
        "nfc_composition_vectors": True,
        "a7cb_distinct_from_open_e": True,
        "nfc_vector": True,
        "sharp_s_collision_rejected": True,
    }


def test_typescript_and_python_match_for_every_unicode_scalar() -> None:
    """Run the real TS verifier across every scalar, not only changed folds."""

    from plotpilot_plugin_sdk.verifier import unicode_nfc_casefold

    digest = hashlib.sha256()
    scalar_count = 0
    for codepoint in range(0x110000):
        if 0xD800 <= codepoint <= 0xDFFF:
            continue
        scalar_count += 1
        digest.update(
            f"{codepoint:06x}\t{unicode_nfc_casefold(chr(codepoint))}\n".encode("utf-8")
        )

    script = r'''
import { createHash } from 'node:crypto'
import { pathToFileURL } from 'node:url'
const verifier = await import(pathToFileURL(process.env.PLOTPILOT_TS_VERIFIER).href)
const digest = createHash('sha256')
let scalarCount = 0
for (let codepoint = 0; codepoint <= 0x10ffff; codepoint += 1) {
  if (codepoint >= 0xd800 && codepoint <= 0xdfff) continue
  scalarCount += 1
  const output = verifier.unicodeNfcCasefold(String.fromCodePoint(codepoint))
  digest.update(`${codepoint.toString(16).padStart(6, '0')}\t${output}\n`, 'utf8')
}
console.log(JSON.stringify({ scalar_count: scalarCount, sha256: digest.digest('hex') }))
'''
    assert _run_ts(script) == {
        "scalar_count": scalar_count,
        "sha256": digest.hexdigest(),
    }


def test_typescript_and_python_match_frozen_nfc_for_every_scalar_nfd_sequence(tmp_path: Path) -> None:
    """Exercise every scalar's NFD sequence against frozen UCD 15.0.0 NFC."""

    from plotpilot_plugin_sdk.package import _unicode_nfc

    vectors_path = tmp_path / "unicode-nfd-vectors.tsv"
    scalar_count = 0
    with vectors_path.open("w", encoding="utf-8", newline="\n") as handle:
        for codepoint in range(0x110000):
            if 0xD800 <= codepoint <= 0xDFFF:
                continue
            scalar_count += 1
            nfd = unicodedata.normalize("NFD", chr(codepoint))
            expected = unicodedata.normalize("NFC", nfd)
            assert _unicode_nfc(nfd) == expected, f"Python frozen NFC mismatch U+{codepoint:04X}"
            nfd_hex = ",".join(f"{ord(character):x}" for character in nfd)
            expected_hex = ",".join(f"{ord(character):x}" for character in expected)
            handle.write(f"{codepoint:06x}\t{nfd_hex}\t{expected_hex}\n")

    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const verifier = await import(pathToFileURL(process.env.PLOTPILOT_TS_VERIFIER).href)
const decode = value => value.split(',').filter(Boolean).map(item => Number.parseInt(item, 16))
const asString = value => String.fromCodePoint(...decode(value))
const lines = readFileSync(process.env.PLOTPILOT_NFD_VECTORS_PATH, 'utf8').trimEnd().split('\n')
let mismatches = []
for (const line of lines) {
  const [rawCodepoint, nfdHex, expectedHex] = line.split('\t')
  const actual = verifier.unicodeNfc(asString(nfdHex))
  const expected = asString(expectedHex)
  if (actual !== expected) {
    mismatches.push({ codepoint: rawCodepoint, actual: [...actual].map(character => character.codePointAt(0).toString(16)), expected: expectedHex })
    if (mismatches.length >= 10) break
  }
}
console.log(JSON.stringify({ scalar_count: lines.length, mismatch_count: mismatches.length, mismatches }))
'''
    assert _run_ts(script, {"PLOTPILOT_NFD_VECTORS_PATH": str(vectors_path)}) == {
        "scalar_count": scalar_count,
        "mismatch_count": 0,
        "mismatches": [],
    }
