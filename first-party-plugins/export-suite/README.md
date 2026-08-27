# PlotPilot Export Suite

`com.plotpilot.export-suite` renders an immutable set of Core current
revisions as EPUB, PDF, DOCX, or Markdown. The plugin never owns or writes
chapter text. Its bytes, MIME type, filename, digest, and source revision
provenance are intended for the real Core Asset port.

This first integration-ready batch is deliberately **domain-only**. P0 does
not yet expose the ordered current-revision query needed by Export, and the
real P1 Asset plus P2/P3 runtime paths are not integrated. Therefore this tree
does not publish a `plugin.json`, wheel, private worker protocol, or false
`artifact-bundle/v1` claim. The blocked runtime slice is recorded under
`coordination/PPA-06/` and will add the release package only against real ports.
