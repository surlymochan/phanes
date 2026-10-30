### Fixed

- Hold every agent on a provider account when its plan window is spent, not only the agents on the
  model that was refused. Before, the other models on that account kept starting turns and failed
  with the same limit. A window the provider reports for one model family, such as the Opus weekly
  window, holds only that family, so the other families on the account keep working.
