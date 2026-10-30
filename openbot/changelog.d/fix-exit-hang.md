### Fixed

- OpenBot now quits when a file operation never finishes, for example when macOS waits for a privacy
  approval that nobody answers. It stops itself at most 10 seconds after its shutdown, which can take
  up to 30 seconds. Before, OpenBot stopped responding after you quit it, and it did not open again
  until you forced it to quit on macOS or ended it on Linux.
