### Added

- An end-to-end check of the transfer file of your agents. It puts a fault into the file system, a
  short write and a write that writes no byte, and holds that the export writes the whole archive
  and that it fails instead of reporting a file that it did not write.

### Fixed

- Exporting your agents to a transfer file no longer writes a broken ZIP. The export wrote every
  chunk of the archive once and took the count of the write for the truth, so a short write left a
  truncated archive that the export reported as a saved file. The export now writes the rest of a
  chunk and fails when a write makes no progress.
