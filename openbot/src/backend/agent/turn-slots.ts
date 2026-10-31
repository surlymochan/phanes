import type { QueueDelivery } from "@openbot/contracts/ipc";

export interface TurnSlotsOptions {
  /** The most turns that run at the same time, or null for no limit. */
  limit(): number | null;
  /**
   * The key of each queue that runs one turn at a time: an agent's own chat, and each of its tasks.
   * A lane key sorts as its agent id does, so a tie in age goes the same way as before tasks.
   */
  lanes(): readonly string[];
  /** Whether the lane runs a turn, starts one, or compacts its context. Each one uses a slot. */
  isRunning(lane: string): boolean;
  /** Whether the lane has a delivery that could start now, if a slot were free. */
  isWaiting(lane: string): boolean;
  /** The delivery at the head of the lane's queue. */
  head(lane: string): SlotQueueDelivery | null;
}

type SlotQueueDelivery = QueueDelivery & { dispatch?: true };

/**
 * Owns the turn slot clause of the queue drain: on a hosted server only a fixed number of turns run
 * at the same time, and each task of an agent uses a slot of its own. When the slots are full, the
 * waiting lanes go by the head of their queue: a message from a person first, then routine runs,
 * teammate messages, and script events, oldest first in each group. A delivery that waits stays
 * queued. It never imports the service.
 *
 * No slot waits on another slot: a teammate request ends the turn that sends it, and the answer
 * arrives as a new delivery.
 */
export class TurnSlots {
  readonly #options: TurnSlotsOptions;

  constructor(options: TurnSlotsOptions) {
    this.#options = options;
  }

  mayStart(lane: string): boolean {
    const limit = this.#options.limit();
    if (limit === null) return true;
    const lanes = this.#options.lanes();
    const free = limit - lanes.filter((id) => this.#options.isRunning(id)).length;
    if (free <= 0) return false;
    const head = this.#options.head(lane);
    if (!head) return true;
    let ahead = 0;
    for (const other of lanes) {
      if (other === lane || this.#options.isRunning(other) || !this.#options.isWaiting(other)) continue;
      const otherHead = this.#options.head(other);
      if (otherHead && startsBefore(otherHead, other, head, lane)) ahead += 1;
    }
    return ahead < free;
  }
}

function startsBefore(left: SlotQueueDelivery, leftLane: string, right: SlotQueueDelivery, rightLane: string): boolean {
  const leftPerson = left.sender.kind === "user" && !left.dispatch;
  const rightPerson = right.sender.kind === "user" && !right.dispatch;
  if (leftPerson !== rightPerson) return leftPerson;
  if (left.createdAt !== right.createdAt) return left.createdAt < right.createdAt;
  return leftLane < rightLane;
}
