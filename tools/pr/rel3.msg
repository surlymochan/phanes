End the watcher of a dispatch run that never finishes

The dispatch run watcher polls with exponential backoff from 2 seconds to 5 minutes, stops after 4 hours, marks a stuck run unknown, does not watch an unknown run again after a restart, and starts again when the run is aborted.
