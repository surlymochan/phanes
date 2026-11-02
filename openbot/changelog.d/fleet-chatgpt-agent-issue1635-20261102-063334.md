### Fixed

- The local organization health review report uses repository-relative paths for file findings and rejects paths outside the selected repository.
- The review pipeline uses the source repository root for nested workspaces. External file findings are rejected with readable check details, without a misleading JSON report.
