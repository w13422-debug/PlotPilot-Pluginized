# PlotPilot Export Suite

`com.plotpilot.export-suite` renders an immutable set of Core current
revisions as EPUB, PDF, DOCX, or Markdown. The plugin never owns or writes
chapter text. Its bytes, MIME type, filename, digest, and source revision
provenance are intended for the real Core Asset port.

The legacy payload media type remains unchanged for the eventual download
route (`text/markdown; charset=utf-8` for Markdown).  At the Asset metadata
boundary the equivalent no-whitespace spelling (`text/markdown;charset=utf-8`)
is used because `asset-metadata/v1` requires a token-like MIME field; the
download bytes and filename are not changed.

`build_export_from_snapshot` verifies the Core `RunSnapshot` and its
`export-current-revisions/v1` parameters Asset, then reads each immutable
chapter Asset through an injected `read_asset` port.  `export_to_asset` uses
only an injected `create_asset` port and emits immutable output metadata plus a
deterministic `export-receipt/v1` projection.  `P1AssetStoreAdapter` is the
explicit bridge for the current P1 `AssetStore.read/describe/put` shape.

This first integration-ready batch is deliberately **domain-only**. P0 does
not yet expose the ordered current-revision query needed by Export, and the
real P1 Asset plus P2/P3 runtime paths are not integrated. Therefore this tree
does not publish a `plugin.json`, wheel, private worker protocol, or false
`artifact-bundle/v1` claim. The blocked runtime slice is recorded under
`coordination/PPA-06/` and will add the release package only against real ports.
