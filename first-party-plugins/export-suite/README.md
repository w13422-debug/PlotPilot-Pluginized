# PlotPilot Export Suite

`com.plotpilot.export-suite` renders an immutable set of Core current
revisions as EPUB, PDF, DOCX, or Markdown. The plugin never owns or writes
chapter text. Its bytes, MIME type, filename, digest, and source revision
provenance are intended for the real Core Asset port.

This first integration-ready batch contains the deterministic domain and the
P0 v1 manifest. P1 Asset persistence/download and the P2/P3 framed runtime
adapter remain external dependency gates.
