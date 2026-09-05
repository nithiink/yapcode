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

export type OrbState = "idle" | "listening" | "working" | "waiting" | "speaking";
export type Target = { x: number; y: number; r: number };

/** The dock's footprint on the right of the stage (its width plus its margin).
 *  She is never allowed to sit behind it: at 1400px the design's 0.45 already
 *  clears the dock, but at ~850px the same proportion puts a third of her
 *  underneath it, and a presence you cannot see is not a presence. */
export const DOCK_W = 450;
/** Breathing room between her edge and the dock. */
const GAP = 24;

/** Where the orb wants to be. `engaged` means something has taken the stage —
 *  a panel, a session tab, the composer with focus — and she yields it.
 *
 *  At home the design's proportions (0.45 across, 0.47 down, 0.34 of the short
 *  side) apply unchanged wherever they fit, and are clamped to the space left
 *  of the dock where they do not. `free` never drops below half the stage: a
 *  window narrow enough for that to bite has already switched to the stacked
 *  layout, where the canvas is hidden entirely. */
export function target(w: number, h: number, engaged: boolean): Target {
  if (engaged) return { x: w - CORNER.dx, y: CORNER.y, r: CORNER.r };
  const free = Math.max(w - DOCK_W, w * 0.5);
  // 0.26 of the short side, down from 0.34. Smaller is what makes true
  // centring possible: at 0.34 a centred orb ran into the dock on anything
  // under a very wide window, so it had to be nudged left permanently.
  const r = Math.min(Math.min(w, h) * 0.26, free * 0.42);
  // Genuinely centred (0.5, not 0.45) wherever it fits — which is now most
  // desktop widths. The clamp still shifts her left rather than let the dock
  // cover her, so on a narrow window she is off-centre instead of hidden;
  // being visible beats being symmetrical.
  return { x: Math.min(w * 0.5, Math.max(free - r - GAP, r + GAP)), y: h * 0.47, r };
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

/** Yuri's colour on the canvas.
 *
 *  Brighter than the `--acc` token (#dd8a6a) the rest of the UI uses, and
 *  deliberately so: the orb is ~1500 translucent 1px squares with per-point
 *  depth attenuation, so the same hex that reads as warm terracotta on a solid
 *  button reads as muddy brown in a point cloud. The token stays the accent
 *  for chrome; this is what that accent has to become to survive the renderer.
 *
 *  Chosen at the brightness ceiling that still holds its colour: saturation
 *  stays at 46%, and past roughly #ffbb93 the hue washes out to cream and
 *  stops being hers at all. Further brightness has to come from alpha, the
 *  depth floor, or point size — not from lightening this.
 */
export const ORB_HUE = "#ffb489";
/** The waiting pulse runs a touch warmer, so "something needs you" is a
 *  temperature change and not only a brightness one — brightness alone is
 *  what the pulse is already spending. */
export const ORB_HUE_WAITING = "#ffb281";

/** How each state looks. `amp` is live audio loudness in 0..1 — the RMS
 *  envelope VoiceProvider already computes from both the microphone and her
 *  own speech, fast attack, slow release. It was written to a CSS variable on
 *  the pre-canvas DOM orb and has gone NOWHERE since the re-shell, which is
 *  most of why every state looked alike: the only thing marking "speaking"
 *  was a 3.2% scale wobble, invisible on a sphere.
 *
 *  Each state gets a distinct KIND of motion, not just a different speed,
 *  because speed alone is not legible on a rotating point cloud:
 *
 *    idle       slow breathing — at rest, not switched off
 *    listening  the cloud TIGHTENS as the user speaks — leaning in
 *    speaking   it SWELLS with her own voice, with a ripple travelling
 *               outward, so the sound looks like it comes from her
 *    working    fast spin plus turbulence — churning
 *    waiting    a strong slow pulse; the one state that costs the user time,
 *               so the one that should catch peripheral vision
 *
 *  `wave` and `waveDepth` are consumed by the renderer as a depth-phased
 *  radial displacement; 0 means no ripple.
 */
export function look(state: OrbState, t: number, amp = 0): {
  hue: string; alpha: number; spin: number; jitter: number; scale: number;
  wave: number; waveDepth: number;
} {
  // NaN has to be caught explicitly: Math.max(0, Math.min(1, NaN)) is NaN,
  // which propagates into every coordinate and draws nothing at all — she
  // would simply vanish. The envelope divides by a buffer length, so a zero
  // there is enough to produce one.
  const a = Number.isFinite(amp) ? Math.max(0, Math.min(1, amp)) : 0;
  const still = { jitter: 0, wave: 0, waveDepth: 0 };

  if (state === "waiting") {
    // Brightness and a slight squash on the same phase, so the pulse reads
    // even where colour does not.
    const pulse = Math.sin(t * 0.045);
    return { ...still, hue: ORB_HUE_WAITING, alpha: 0.82 + pulse * 0.18,
             spin: 0.0028, scale: 1 + pulse * 0.035 };
  }

  if (state === "speaking") {
    return { hue: ORB_HUE,
             // Floored, so a quiet passage does not make her vanish mid-sentence.
             alpha: 0.86 + a * 0.14,
             spin: 0.0075, jitter: 0,
             // Real loudness rather than a fixed sine. This is the fix.
             scale: 1 + a * 0.18,
             wave: 0.03 + a * 0.06, waveDepth: t * 0.11 };
  }

  if (state === "listening") {
    return { ...still, hue: ORB_HUE, alpha: 0.8 + a * 0.2, spin: 0.004,
             // Contracts where speaking swells — the opposite gesture, so the
             // two can never be mistaken for each other.
             scale: 1 - a * 0.07 };
  }

  if (state === "working") {
    return { ...still, hue: ORB_HUE, alpha: 1, spin: 0.0125, jitter: 0.02, scale: 1 };
  }

  // idle: small and slow, but motion — she should not read as a frozen image.
  return { ...still, hue: ORB_HUE, alpha: 0.88, spin: 0.0028,
           scale: 1 + Math.sin(t * 0.016) * 0.02 };
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
  // The user talking is worth showing. Without it she looks identical whether
  // she is hearing them or ignoring them, which is the least reassuring thing
  // a listening interface can do.
  if (vstate === "listening" || vstate === "hearing") return "listening";
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

/** A slow wander of her centre, in pixels, so she is never perfectly still.
 *
 *  Two incommensurable frequencies per axis, so the path never visibly loops —
 *  a repeating drift reads as an animation, which is the opposite of the
 *  intended effect. Amplitude is a fraction of her radius rather than a fixed
 *  pixel count, so she drifts the same *relative* amount in the corner as at
 *  centre; a fixed offset that is a gentle sway at 260px is a twitch at 54.
 *
 *  Deliberately tiny. This should be the difference between a photograph and
 *  a held breath, not something the user can point at.
 */
export function drift(t: number, radius: number): { dx: number; dy: number } {
  const a = radius * 0.028;
  return {
    dx: (Math.sin(t * 0.0043) + Math.sin(t * 0.0011) * 0.6) * a,
    dy: (Math.cos(t * 0.0037) + Math.sin(t * 0.0009) * 0.6) * a,
  };
}

/** How much of the previous frame survives into this one, 0..1.
 *
 *  The canvas is faded rather than cleared, so motion leaves a wake — the
 *  glide to the corner becomes a comet, and her breathing has a soft edge
 *  instead of a hard one. Speaking holds a longer trail because that is when
 *  she is most alive; `waiting` holds almost none, because a pulse meant to
 *  catch the eye needs crisp edges to do it.
 */
export function persistence(state: OrbState): number {
  if (state === "speaking") return 0.55;
  if (state === "waiting") return 0.15;
  if (state === "working") return 0.45;
  return 0.35;
}
