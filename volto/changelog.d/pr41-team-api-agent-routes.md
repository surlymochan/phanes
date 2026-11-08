### Fixed

- A client that saves the profile of an agent that it may not see gets "Agent not found." Before, it
  got "You cannot change this agent."
- A client that calls a path or a method that the host does not serve gets "Route not found." for
  every member. Before, a member without the manager role got "You cannot change this agent."
- A client that saves an empty password from the secure card of an agent gets "Invalid secure
  authentication response." Before, the host answered "Request failed." and wrote an unexpected
  error to its log.
