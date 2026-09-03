// The orb's geometry and state derivation, kept pure so `node --test` can
// reach it. The component (components/shell/Orb.tsx) owns only the canvas and
// the animation frame; every number it draws with comes from here.
//
// The orb is Yuri, singular. There is never one per session and never a
// background constellation — sessions live in the dock's tabs, and provider
// colour lives on those tabs. See docs/yuri/design/README.md.

/** How much of the way to the target each frame moves. The motion is an eased
 *  lerp rather than a CSS transition: a transition animates between two
 *  committed values, so a target that moves mid-flight restarts it. Lerping
 *  every frame toward a live target is what makes her read as alive. */
export const EASE = 0.075;

/** Corner size in flat px, deliberately NOT a viewport proportion: in the
 *  corner she is chrome, and chrome should not grow with the window. */
export const CORNER = { dx: 92, y: 130, r: 54 };

export type OrbState = "idle" | "working" | "waiting" | "speaking";
export type Target = { x: number; y: number; r: number };

/** Where the orb wants to be. `engaged` means something has taken the stage —
 *  a panel, a session tab, the composer with focus — and she yields it. */
export function target(w: number, h: number, engaged: boolean): Target {
  if (engaged) return { x: w - CORNER.dx, y: CORNER.y, r: CORNER.r };
  return { x: w * 0.45, y: h * 0.47, r: Math.min(w, h) * 0.34 };
}

/** One lerp step. Position and radius move together — a separate radius
 *  easing makes her arrive at the corner still growing. */
export function step(pos: Target, to: Target, ease = EASE): Target {
  return {
    x: pos.x + (to.x - pos.x) * ease,
    y: pos.y + (to.y - pos.y) * ease,
    r: pos.r + (to.r - pos.r) * ease,
  };
}

/** A Fibonacci sphere: even coverage without the pole clustering of a
 *  lat/long grid, which reads as a seam when the cloud rotates. */
export function sphere(n: number): [number, number, number][] {
  const out: [number, number, number][] = [];
  const ga = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = n === 1 ? 0 : 1 - (i / (n - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const th = ga * i;
    out.push([Math.cos(th) * r, y, Math.sin(th) * r]);
  }
  return out;
}

/** Per-state look. Only `waiting` pulses, because it is the one state that is
 *  costing the user time; making `working` pulse too would spend the signal on
 *  the state they are happy to leave alone. Hue stays hers in every state — a
 *  provider colour on the orb would make her look like a session. */
export function look(state: OrbState, t: number): {
  hue: string; alpha: number; spin: number; jitter: number; scale: number;
} {
  const spin = state === "working" ? 0.0125 : state === "speaking" ? 0.0075 : 0.0028;
  if (state === "waiting") {
    return { hue: "#d9906a", alpha: 0.62 + Math.sin(t * 0.045) * 0.3, spin, jitter: 0, scale: 1 };
  }
  return {
    hue: "#dd8a6a",
    alpha: state === "idle" ? 0.52 : 0.92,
    spin,
    jitter: state === "working" ? 0.012 : 0,
    scale: state === "speaking" ? 1 + Math.sin(t * 0.075) * 0.032 : 1,
  };
}

/** Her state, in priority order: a decision waiting on the user outranks her
 *  own speech, which outranks work in flight. Anything needing a human is the
 *  only thing that costs time by going unnoticed, so it wins. */
export function orbState(
  vstate: string,
  sessions: { status?: string; running?: boolean }[],
  approvalCount: number,
): OrbState {
  if (approvalCount > 0 || sessions.some((s) => s.status === "needs_permission" || s.status === "needs_choice"))
    return "waiting";
  if (vstate === "speaking") return "speaking";
  // `running` is a turn executing right now. `status === "running"` is NOT
  // that — it means the session process is alive, which is true of every idle
  // session sitting at a prompt. Keying "working" off the status made her spin
  // as though busy whenever anything was merely open.
  if (sessions.some((s) => s.running)) return "working";
  return "idle";
}

/** Points to draw. The full cloud is 1500; a device that told us it prefers
 *  reduced motion, or one without the pixel budget, gets the cheaper cloud
 *  rather than a stuttering one. */
export function pointCount(reducedMotion: boolean, cores: number): number {
  if (reducedMotion) return 500;
  return cores <= 4 ? 600 : 1500;
}
