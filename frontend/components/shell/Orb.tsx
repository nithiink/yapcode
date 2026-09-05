"use client";

// Yuri herself: a rotating point cloud on a full-stage canvas. Every number
// comes from lib/orb.ts; this file owns the canvas, the frame loop, and the
// two things that keep it from burning a laptop battery — a cloud sized to
// the machine, and a loop that stops when nothing can see it.
import { useEffect, useRef } from "react";
import { drift, look, orbState, persistence, pointCount, sphere, step, target,
         type Target } from "@/lib/orb.ts";
import { useYuri } from "@/components/VoiceProvider";

function rgb(hex: string): [number, number, number] {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

export function Orb({ engaged }: { engaged: boolean }) {
  const cvRef = useRef<HTMLCanvasElement>(null);
  const { vstate, sessions, approvals, ampRef } = useYuri();

  // The loop reads its inputs through a ref rather than restarting on every
  // change: sessions refresh on a 2.5s poll, and a loop that tore down and
  // rebuilt itself that often would drop the eased position mid-flight and
  // snap her to the new target.
  const live = useRef({ engaged, vstate, sessions, approvals });
  live.current = { engaged, vstate, sessions, approvals };

  useEffect(() => {
    const cv = cvRef.current;
    const cx = cv?.getContext("2d");
    if (!cv || !cx) return;

    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    // The canvas is FADED rather than cleared each frame, so motion leaves a
    // wake. That needs the page's own background colour, read from the token
    // so it cannot drift from it — a hardcoded hex here would show up as a
    // faint rectangle the day --bg changes.
    const bg = (getComputedStyle(document.documentElement).getPropertyValue("--bg") || "#1a1917").trim();
    const pts = sphere(pointCount(reduced, navigator.hardwareConcurrency || 8));

    let w = 0, h = 0;
    const size = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = cv.clientWidth;
      h = cv.clientHeight;
      cv.width = w * dpr;
      cv.height = h * dpr;
      cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    size();
    window.addEventListener("resize", size);

    // null until the first frame: seeding at the target rather than at the
    // origin stops her flying in from the top-left corner on every mount.
    let pos: Target | null = null;
    let t = 0;
    let raf = 0;

    const frame = () => {
      t += 1;
      const s = live.current;
      const to = target(w, h, s.engaged);
      pos = pos ? step(pos, to) : to;

      // Publish her HOME centre as a CSS variable so the naming block under
      // her can be positioned from it. Both used to hard-code the same 0.45
      // and drifted apart the moment target() started clamping for the dock:
      // her name ended up under the orb's left edge. This is the one link
      // that cannot go stale. `engaged` is excluded on purpose — the block
      // fades out then, and following her to the corner would drag it along
      // as it went.
      if (!s.engaged) {
        const home = target(w, h, false);
        cv.parentElement?.style.setProperty("--orb-x", `${Math.round(home.x)}px`);
      }

      const st = orbState(s.vstate, s.sessions, s.approvals.length);

      // Fade, don't clear: the previous frame decays instead of vanishing, so
      // the glide to the corner draws a comet and her breathing has a soft
      // edge. `reduced` opts out entirely — a persistent trail is motion, and
      // someone who asked for less of it means this too.
      if (reduced) {
        cx.clearRect(0, 0, w, h);
      } else {
        cx.globalAlpha = 1 - persistence(st);
        cx.fillStyle = bg;
        cx.fillRect(0, 0, w, h);
        cx.globalAlpha = 1;
      }
      // Live loudness, straight off the analyser's envelope. Read through the
      // ref rather than through props: this changes ~60 times a second and
      // must never cause a React render.
      const amp = ampRef.current ?? 0;
      const { hue, alpha, spin, jitter, scale, wave, waveDepth } = look(st, t, amp);
      // A slow wander, so she is never perfectly still. Applied to the drawn
      // position only — never to `pos` itself, or the lerp would chase the
      // drift and the two would compound into a wobble.
      const dr = reduced ? { dx: 0, dy: 0 } : drift(t, pos.r);
      const cxp = pos.x + dr.dx;
      const cyp = pos.y + dr.dy;

      // Her core. A uniform shell of points reads as an object; something
      // brighter at the centre reads as a thing with an inside. It breathes
      // with her voice, which is the whole reason it is here.
      const coreR = pos.r * (0.42 + amp * 0.22);
      const g = cx.createRadialGradient(cxp, cyp, 0, cxp, cyp, coreR);
      const [cr0, cg0, cb0] = rgb(hue);
      g.addColorStop(0, `rgba(${cr0},${cg0},${cb0},${(0.16 + amp * 0.2).toFixed(3)})`);
      g.addColorStop(0.55, `rgba(${cr0},${cg0},${cb0},${(0.05 + amp * 0.07).toFixed(3)})`);
      g.addColorStop(1, `rgba(${cr0},${cg0},${cb0},0)`);
      cx.fillStyle = g;
      cx.beginPath();
      cx.arc(cxp, cyp, coreR, 0, Math.PI * 2);
      cx.fill();
      const [cr, cg, cb] = rgb(hue);
      const rot = t * spin;
      const ca = Math.cos(rot), sa = Math.sin(rot);
      const ct = Math.cos(0.42), stl = Math.sin(0.42);   // fixed axial tilt

      for (let i = 0; i < pts.length; i++) {
        const [x, y, z] = pts[i];
        const x1 = x * ca - z * sa;
        const z1 = x * sa + z * ca;
        const y1 = y * ct - z1 * stl;
        const z2 = y * stl + z1 * ct;
        // Weak perspective (d = 1.9) so the near face reads as nearer without
        // the cloud looking like a fisheye lens.
        const k = 1.9 / (1.9 - z2 * 0.55);
        // Turbulence (working) and the speaking ripple are both per-point
        // radial displacements, so they compose into one multiplier. The
        // ripple is phased BY DEPTH, which is what makes it read as a wave
        // travelling out through the cloud rather than the whole sphere
        // throbbing in unison.
        let disp = 1;
        if (jitter) disp += Math.sin(t * 0.05 + i * 0.7) * jitter;
        if (wave) disp += Math.sin(waveDepth - z2 * 3.2) * wave;
        const sx = cxp + x1 * pos.r * k * disp * scale;
        const sy = cyp + y1 * pos.r * k * disp * scale;
        // Depth keys both brightness and size: that pairing is what makes a
        // flat scatter of squares read as a sphere.
        const depth = (z2 + 1) / 2;
        // The far side keeps a real floor rather than fading to almost
        // nothing: at 0.16 the back of the sphere was a sixth of an already
        // sub-1 alpha, which is what made her read as dim rather than distant.
        // The near/far ratio still carries the depth cue.
        const a = alpha * (0.45 + depth * 0.55);
        // Size is the one brightness lever that costs no saturation: more lit
        // pixels rather than paler ones. Depth still drives it, so the cue
        // survives — the near face is simply drawn heavier than it was.
        const px = 0.85 + depth * 1.25;
        cx.fillStyle = `rgba(${cr},${cg},${cb},${a.toFixed(3)})`;
        cx.fillRect(sx, sy, px, px);
      }
      raf = requestAnimationFrame(frame);
    };

    // A hidden tab still services requestAnimationFrame in some browsers, and
    // 1500 points a frame is not a background cost worth paying.
    const visibility = () => {
      cancelAnimationFrame(raf);
      if (!document.hidden) raf = requestAnimationFrame(frame);
    };
    document.addEventListener("visibilitychange", visibility);
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", size);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);

  return <canvas ref={cvRef} className="orb-canvas" aria-hidden="true" />;
}
