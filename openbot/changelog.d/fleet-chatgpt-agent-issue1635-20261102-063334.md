### Fixed

- The local organization health review report uses repository-relative paths for file findings and rejects paths outside the selected repository.
- The local review pipeline selects its output workspace as the report root, so metadata findings remain readable when outputs are stored outside the source repository.
