# NW-P1-BDP-F-011 minimal structural remediation

The clean Sol rereview found one remaining ordering defect: `_verify_bundle`
read `backup.json` directly before the existing control-file boundary and
reparse checks in `_read_canonical_json` ran.

The minimal correction removes that early read. `_verify_bundle` first obtains
the manifest through `_read_canonical_json`, whose existing helper rejects
reparse components and the control file itself before reading. Because that
function also proves the bytes are canonical, `manifest_raw` is then safely
reconstructed with `_canonical_json(manifest)` for receipt SHA-256 binding.

The decisive negative test marks `backup.json` as a reparse control file and
spies on `Path.read_bytes`. Verification must raise `BackupValidationError` and
the spy must observe zero reads of the rejected control file. The same helper
path is platform-neutral and retains the existing Windows junction/reparse and
non-Windows symlink behavior.

No other Finding, public contract, runtime composition port, or dependency was
changed. `NW-P1-BDP-F-011` remains pending the clean Sol reviewer; this source
task does not claim closure or merge eligibility.
