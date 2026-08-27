from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from tools.integration import validate_merge_gate as gate
BASE = "42123d1a5126bb2bef31304b0498e2e7def9183e"
PUBLICATION_HEAD = "745ba10c7c714b29ef43289a9096d2e64b2eb5bd"
IMPLEMENTATION_HEAD = "63f8a0e863afe77e70aef3d0f6687039c6cfb7ec"
MATRIX = Path(
    r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\project-matrix.json"
)
MATRIX_SHA256 = "076541384c643d879e8552737f94a9ce2ed9134dba3e0d448f2c4a0b58297656"
MANIFEST_SHA256 = "cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024"

BATCHES = [
    ("P2", "5daece82633a700876474b6468f9948f62d466d7", "745ba10c7c714b29ef43289a9096d2e64b2eb5bd", "e753506c94b5310d0ec3f70c5dbd7a95ca5f761a"),
    ("P3", "3196aa7cccc9af279107885cc4cea8c26a785020", "e753506c94b5310d0ec3f70c5dbd7a95ca5f761a", "9979fa544932d9c947f8fa9e163c79a1b502c132"),
    ("P4", "86208cc2b6bcd421767eea206532f62cdff36800", "9979fa544932d9c947f8fa9e163c79a1b502c132", "4f49279108122d8a9f2450894aff32f12a4936d6"),
    ("P5", "0d9de43acef1ba4d83ed37dbc0e0b6eb44c1ad15", "4f49279108122d8a9f2450894aff32f12a4936d6", "8825337bb25b20a3567184892bb4de80259b7f8e"),
    ("P6", "b603b40635f174b984efade18add7afd40ed3db9", "8825337bb25b20a3567184892bb4de80259b7f8e", "63f8a0e863afe77e70aef3d0f6687039c6cfb7ec"),
]

FORBIDDEN = {
    "P2": "be9e7273ed697d25a05454c17810b0872a8fe3cf",
    "P4": "2bbdc0efca047e388494b59bbe4ff4fcd34fcec6",
    "P5": "1262c2fda32b357596ce67d1ff3cdcc406ac13b5",
    "P6": "90929ea91b944cc5e22483d21631d806f9aed208",
}

TAGS = {
    "M0-OPEN": ("0a527454a2e9d20e353c41391d53d734a6dd62dc", "786986a02219973a69a0d1c1114191149924d7f7"),
    "M0-OPEN-R2": ("2b375224ec4762b9b25bca10cb80035f715b0b6f", "36ca847d8b328e3f4f7791c836e0530e8a5ea5b3"),
    "M0-OPEN-R3": ("9c7dbb76b0ce63c55b922e7ee629f25d229b9269", "b6e20df15de9eb4b25de1acfe4e8a8f9e7080bde"),
    "M0-OPEN-R4": ("385bc499032910195081e83a270b59c539f33539", "42123d1a5126bb2bef31304b0498e2e7def9183e"),
}


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        text=True,
        encoding="utf-8",
        errors="strict",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def value(*args: str) -> str:
    return git(*args).stdout.strip()


def ancestor(older: str, newer: str) -> bool:
    result = git("merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr.strip())
    return result.returncode == 0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    assert value("branch", "--show-current") == "codex/ppa-00-integration"
    assert value("rev-parse", "HEAD") == IMPLEMENTATION_HEAD
    assert not value("status", "--porcelain=v1", "--untracked-files=all")
    assert value("remote", "get-url", "--push", "donor-local") == "DISABLED"
    assert sha256(MATRIX) == MATRIX_SHA256
    assert sha256(ROOT / "contracts" / "manifest-v1.json") == MANIFEST_SHA256
    assert ancestor(PUBLICATION_HEAD, IMPLEMENTATION_HEAD)

    matrix = json.loads(MATRIX.read_text(encoding="utf-8-sig"))
    expected_union: set[str] = set()
    batch_results = []
    for project_id, source, pre_head, merge_commit in BATCHES:
        assert value("cat-file", "-t", source) == "commit"
        assert ancestor(BASE, source)
        source_paths = gate._commit_changed_paths(ROOT, BASE, source)
        project = gate._downstream_project(matrix, project_id)
        out_of_set = [
            path
            for path in source_paths
            if not any(gate._pattern_matches(path, pattern) for pattern in project["write_sets"])
        ]
        assert not out_of_set
        assert not expected_union.intersection(source_paths)
        expected_union.update(source_paths)

        parents = value("show", "-s", "--format=%P", merge_commit).split()
        assert parents == [pre_head, source]
        assert ancestor(merge_commit, IMPLEMENTATION_HEAD)
        batch_results.append(
            {
                "project_id": project_id,
                "source": source,
                "source_tree": value("show", "-s", "--format=%T", source),
                "source_changed_paths": len(source_paths),
                "out_of_set_paths": out_of_set,
                "merge_commit": merge_commit,
                "merge_parents": parents,
                "merge_tree": value("show", "-s", "--format=%T", merge_commit),
            }
        )

    integrated_paths = set(gate._commit_changed_paths(ROOT, PUBLICATION_HEAD, IMPLEMENTATION_HEAD))
    assert integrated_paths == expected_union

    forbidden_results = {}
    for project_id, commit in FORBIDDEN.items():
        assert value("cat-file", "-t", commit) == "commit"
        included = ancestor(commit, IMPLEMENTATION_HEAD)
        assert not included
        forbidden_results[project_id] = {"commit": commit, "is_ancestor": included}

    p1_merge = "0239ea6cf3d94b1991c68b9ef9671b33319c2bc0"
    p1_source = "77e836d126e5be525e95b34f2f396ac7b9f864a1"
    assert ancestor(p1_merge, IMPLEMENTATION_HEAD)
    p1_parent_uses = sum(
        1
        for line in value("rev-list", "--parents", IMPLEMENTATION_HEAD).splitlines()
        if p1_source in line.split()[1:]
    )
    assert p1_parent_uses == 1

    tag_results = {}
    for tag, (tag_object, peeled) in TAGS.items():
        actual = (value("rev-parse", tag), value("rev-parse", f"{tag}^{{}}"))
        assert actual == (tag_object, peeled)
        tag_results[tag] = {"tag_object": actual[0], "peeled_commit": actual[1]}

    result = {
        "schema": "p0-p2-p6-object-write-set-gate/v1",
        "passed": True,
        "implementation_head": IMPLEMENTATION_HEAD,
        "implementation_tree": value("show", "-s", "--format=%T", IMPLEMENTATION_HEAD),
        "publication_head": PUBLICATION_HEAD,
        "base": BASE,
        "matrix_sha256": MATRIX_SHA256,
        "contract_manifest_sha256": MANIFEST_SHA256,
        "integrated_changed_paths": len(integrated_paths),
        "union_matches_sources": True,
        "source_path_overlap_count": 0,
        "batches": batch_results,
        "forbidden_evidence_only": forbidden_results,
        "p1_merge_ancestor": True,
        "p1_source_parent_use_count": p1_parent_uses,
        "old_tags": tag_results,
        "donor_push": "DISABLED",
        "worktree_clean": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
