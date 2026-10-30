### Added

- An end-to-end check of the transfer file with files. It holds that the archive holds every file
  with its bytes and its mode, that a file that is deleted while the export runs is reported and
  left out, and that a file of 64 MB is exported without holding it in the memory.

### Fixed

- Exporting your agents to a transfer file no longer holds a whole file in the memory of the app.
  The export read every file at once, so a large file used much memory and could slow the app
  down. The export now reads every file in small pieces and writes them to the archive one piece
  at a time. The file itself is the same.
