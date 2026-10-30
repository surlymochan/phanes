/**
 * How much memory the computer that runs OpenBot has left. Only a server gives one (hosted,
 * self-hosted or a container): one unit or container holds OpenBot, its browser tabs and every
 * provider process. The main process reads the system; this file has no Electron import, so the
 * backend can use it.
 */
export type HostMemoryLevel = "ok" | "low" | "critical";

export interface HostMemory {
  level(): HostMemoryLevel;
  /** The most agent turns that run at the same time on this machine. */
  turnLimit(): number;
  /**
   * Counts the memory of a turn that starts now, before its processes grow. Returns the release, for
   * a start that ends with no turn.
   */
  reserveTurn(): () => void;
  /** Called after each sample. Returns the unsubscribe. */
  subscribe(listener: () => void): () => void;
  /** True on a hosted server: its plan sets the memory, so the low-memory notice names the plan. */
  readonly hostedPlan: boolean;
}
