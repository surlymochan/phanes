### Fixed

- The pre-commit hook finds the project by its own location, so its checks run in a checkout where
  volto is a subdirectory of a larger repository and in a linked worktree.
