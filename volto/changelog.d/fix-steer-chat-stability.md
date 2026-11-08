### Fixed

- A long agent chat no longer loses the steer message, the plan or the reply of a running turn when
  a routine marker follows the turn. Before, these rows showed only after the chat was opened again,
  and then moved the chat.
- A reply that streams into a running turn now stays at its place in the turn. Before, it moved up
  when the next update arrived.
- The new messages button now counts the rows that arrive above the newest row.
