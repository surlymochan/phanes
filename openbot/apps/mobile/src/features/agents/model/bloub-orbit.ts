import { BotEngine } from "@norbert_bodziony/bloub";
import { makeMutable } from "react-native-reanimated";

/**
 * The orbit rings that the desktop turns around a working agent (`OrbitRings` in
 * `packages/ui/src/features/agents/AgentAvatar.tsx`). The rings do not depend on the agent, so one
 * cycle is sampled once and every avatar draws from it.
 */

/** The desktop draws the rings at 30 frames per second (`AVATAR_FPS`). */
const ORBIT_FPS = 30;
/** The `orbit` pose fades every ring out by 3.6 s. The desktop then plays it again from 0. */
export const ORBIT_CYCLE = 3.6;
/** The desktop starts at the pose's own start, 1.2 s, where every ring shows. */
const ORBIT_START = 1.2;
const FRAME_TOTAL = Math.round(ORBIT_CYCLE * ORBIT_FPS);
const BATCH = 6;

export interface OrbitRing {
  id: string;
  /** The stroke width, in units of the avatar view box. */
  width: number;
  /** The colours along the ring. They do not change while it turns. */
  gradient: { x1: number; y1: number; x2: number; y2: number; stops: string[] };
}

/** One ring in one frame: the half behind the body, the half in front, and its fade. */
export interface OrbitArc {
  front: string;
  back: string;
  opacity: number;
}

const NO_ARC: OrbitArc = { front: "", back: "", opacity: 0 };

let rings: OrbitRing[] | null = null;

/** The six rings, in the order of every frame. */
export function orbitRings(): OrbitRing[] {
  rings ??= new BotEngine(100, "orbit")
    .sample(ORBIT_START)
    .arcs.map((arc) => ({ id: arc.id, width: arc.width, gradient: arc.grad }));
  return rings;
}

/** One cycle, `[frame][ring]`, on the UI thread. Empty until `prepareOrbitFrames` finishes. */
export const orbitFrames = makeMutable<OrbitArc[][]>([]);

let preparing = false;

/** Samples the cycle in idle time, once for the app. A ring draws nothing until it is ready. */
export function prepareOrbitFrames(): void {
  if (preparing) return;
  preparing = true;
  const ids = orbitRings().map((ring) => ring.id);
  const engine = new BotEngine(100, "orbit");
  const frames: OrbitArc[][] = [];
  const batch = () => {
    for (let count = 0; count < BATCH && frames.length < FRAME_TOTAL; count += 1) {
      const arcs = engine.sample(frames.length / ORBIT_FPS).arcs;
      frames.push(
        ids.map((id) => {
          const arc = arcs.find((candidate) => candidate.id === id);
          return arc ? { front: arc.front, back: arc.back, opacity: arc.opacity } : NO_ARC;
        }),
      );
    }
    if (frames.length < FRAME_TOTAL) requestIdleCallback(batch);
    else orbitFrames.set(frames);
  };
  requestIdleCallback(batch);
}

/** The frame to draw `clock` seconds after the rings appeared. */
export function orbitFrameIndex(clock: number, count: number): number {
  "worklet";
  return Math.floor(((clock + ORBIT_START) % ORBIT_CYCLE) * ORBIT_FPS) % count;
}
