### Changed

- A routine whose answer is unchanged for three runs in a row waits longer before it runs again.
  The wait starts at one hour and doubles at each further unchanged run, up to four hours. A
  changed answer ends the wait. Before, every due run used a model turn, even when nothing had
  changed.
- While the answer stays unchanged and the agent's thread has no new message, a due run is skipped
  and uses no tokens.
