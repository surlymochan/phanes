/**
 * The X display the agents work on, when it is not the display OpenBot shows its windows on.
 *
 * The server scripts name it in `OPENBOT_AGENT_DISPLAY`: the boat desktop on a hosted server, else a
 * second virtual display. OpenBot keeps its own `DISPLAY`, because its built-in browser draws its
 * tabs in the OpenBot window. The Computer Use driver, remote desktop and the agent processes get
 * the agent display, so an agent's screenshot does not show OpenBot, remote desktop shows what the
 * agent works on, and a program an agent starts opens where the driver can see it.
 *
 * On a computer with a person at it, nothing sets the variable and everything shares one display.
 */
export function agentDisplayEnvironment(
  platform: NodeJS.Platform,
  environment: NodeJS.ProcessEnv = process.env,
): Readonly<Record<string, string>> {
  const display = platform === "linux" ? environment.OPENBOT_AGENT_DISPLAY : undefined;
  return display ? { DISPLAY: display } : {};
}

/** Whether the agents have a display of their own, which OpenBot's windows are not on. */
export function hasSeparateAgentDisplay(platform: NodeJS.Platform, environment: NodeJS.ProcessEnv = process.env) {
  return "DISPLAY" in agentDisplayEnvironment(platform, environment);
}
