Fix usage-partial stop and held turn after interrupts

A partial or family-scoped usage limit after work no longer creates an
account-wide pause or hold. The turn ends as failed. The held turn of an
interrupted target is kept as it was before the upstream sync.
