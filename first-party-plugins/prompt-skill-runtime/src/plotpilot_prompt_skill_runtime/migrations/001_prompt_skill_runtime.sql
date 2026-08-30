CREATE TABLE p2_skill_release(
    skill_id TEXT NOT NULL,
    release_id TEXT NOT NULL CHECK(length(release_id)=64 AND release_id NOT GLOB '*[^0-9a-f]*'),
    package_hash TEXT NOT NULL CHECK(length(package_hash)=64 AND package_hash NOT GLOB '*[^0-9a-f]*'),
    version TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    files_manifest_sha256 TEXT NOT NULL CHECK(length(files_manifest_sha256)=64 AND files_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(skill_id,release_id),
    UNIQUE(skill_id,release_id,package_hash)
);

CREATE TABLE p2_skill_active(
    skill_id TEXT PRIMARY KEY,
    active_release_id TEXT NOT NULL,
    package_hash TEXT NOT NULL CHECK(length(package_hash)=64 AND package_hash NOT GLOB '*[^0-9a-f]*'),
    source TEXT NOT NULL CHECK(source IN ('system','user','legacy')),
    revision INTEGER NOT NULL CHECK(revision>=1),
    FOREIGN KEY(skill_id,active_release_id,package_hash)
        REFERENCES p2_skill_release(skill_id,release_id,package_hash)
);

CREATE TABLE p2_skill_chain(
    chain_id TEXT PRIMARY KEY,
    run_snapshot_hash TEXT NOT NULL CHECK(length(run_snapshot_hash)=64 AND run_snapshot_hash NOT GLOB '*[^0-9a-f]*'),
    input_hash TEXT NOT NULL CHECK(length(input_hash)=64 AND input_hash NOT GLOB '*[^0-9a-f]*'),
    final_output_hash TEXT CHECK(final_output_hash IS NULL OR (length(final_output_hash)=64 AND final_output_hash NOT GLOB '*[^0-9a-f]*')),
    chain_hash TEXT NOT NULL CHECK(length(chain_hash)=64 AND chain_hash NOT GLOB '*[^0-9a-f]*'),
    chain_json TEXT NOT NULL CHECK(json_valid(chain_json))
);

CREATE TABLE p2_skill_receipt(
    receipt_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL REFERENCES p2_skill_chain(chain_id),
    chain_index INTEGER NOT NULL CHECK(chain_index>=0),
    skill_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    package_hash TEXT NOT NULL CHECK(length(package_hash)=64 AND package_hash NOT GLOB '*[^0-9a-f]*'),
    input_asset_id TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK(length(input_hash)=64 AND input_hash NOT GLOB '*[^0-9a-f]*'),
    output_asset_id TEXT,
    output_hash TEXT CHECK(output_hash IS NULL OR (length(output_hash)=64 AND output_hash NOT GLOB '*[^0-9a-f]*')),
    claim_asset_id TEXT,
    claim_asset_hash TEXT CHECK(claim_asset_hash IS NULL OR (length(claim_asset_hash)=64 AND claim_asset_hash NOT GLOB '*[^0-9a-f]*')),
    receipt_hash TEXT NOT NULL CHECK(length(receipt_hash)=64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    validation_context_json TEXT NOT NULL CHECK(json_valid(validation_context_json)),
    UNIQUE(chain_id,chain_index),
    FOREIGN KEY(skill_id,release_id,package_hash)
        REFERENCES p2_skill_release(skill_id,release_id,package_hash)
);

CREATE INDEX p2_skill_receipt_chain_order ON p2_skill_receipt(chain_id,chain_index);
CREATE INDEX p2_skill_release_package ON p2_skill_release(skill_id,package_hash,release_id);

CREATE TRIGGER p2_skill_release_no_update BEFORE UPDATE ON p2_skill_release
BEGIN SELECT RAISE(ABORT,'p2_skill_release is immutable'); END;
CREATE TRIGGER p2_skill_release_no_delete BEFORE DELETE ON p2_skill_release
BEGIN SELECT RAISE(ABORT,'p2_skill_release is immutable'); END;
CREATE TRIGGER p2_skill_chain_no_update BEFORE UPDATE ON p2_skill_chain
BEGIN SELECT RAISE(ABORT,'p2_skill_chain is immutable'); END;
CREATE TRIGGER p2_skill_chain_no_delete BEFORE DELETE ON p2_skill_chain
BEGIN SELECT RAISE(ABORT,'p2_skill_chain is immutable'); END;
CREATE TRIGGER p2_skill_receipt_no_update BEFORE UPDATE ON p2_skill_receipt
BEGIN SELECT RAISE(ABORT,'p2_skill_receipt is immutable'); END;
CREATE TRIGGER p2_skill_receipt_no_delete BEFORE DELETE ON p2_skill_receipt
BEGIN SELECT RAISE(ABORT,'p2_skill_receipt is immutable'); END;
