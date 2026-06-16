// Pure helpers for the voice orb's volume animation, extracted from the React
// component so the gate timing is unit-testable (see orb.test.ts).
//
// The orb scales with live audio: each rAF frame we read the time-domain samples
// from every active analyser (the user's mic and the assistant's speech), take
// the loudest as the instantaneous target, then envelope-smooth it. The gate
// keeps the target pinned at 0 until the session is genuinely ready — the mic
// analyser attaches during the connection handshake, so without the gate the orb
// would scale from mic input before the agent can actually hear/respond.

// RMS amplitude of one analyser's byte time-domain buffer, scaled into 0..1.
export function rmsAmp(buf: Uint8Array | number[]): number {
  let sum = 0;
  for (let i = 0; i < buf.length; i++) {
    const v = (buf[i] - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / buf.length) * 3.2);
}

// Instantaneous orb target for one frame. Returns 0 when the gate is closed
// (still connecting), otherwise the loudest analyser's RMS.
export function orbTarget(gateOpen: boolean, bufs: Array<Uint8Array | number[]>): number {
  if (!gateOpen) return 0;
  let target = 0;
  for (const buf of bufs) {
    const rms = rmsAmp(buf);
    if (rms > target) target = rms; // loudest source wins
  }
  return target;
}

// Envelope step: snap up quickly (fast attack), ease down slowly (slow release)
// so the orb rises lively and falls gently instead of twitching frame-to-frame.
export function envelopeStep(smoothed: number, target: number): number {
  const k = target > smoothed ? 0.35 : 0.08;
  return smoothed + (target - smoothed) * k;
}
