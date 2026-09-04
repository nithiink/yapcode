"use client";

// Yuri herself: a rotating point cloud on a full-stage canvas. Every number
// comes from lib/orb.ts; this file owns the canvas, the frame loop, and the
// two things that keep it from burning a laptop battery — a cloud sized to
// the machine, and a loop that stops when nothing can see it.
import { useEffect, useRef } from "react";
import { look, orbState, pointCount, sphere, step, target, type Target } from "@/lib/orb.ts";
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
  const { vstate, sessions, approvals } = useYuri();

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
      cx.clearRect(0, 0, w, h);
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
      const { hue, alpha, spin, jitter, scale } = look(st, t);
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
        const wob = jitter ? 1 + Math.sin(t * 0.05 + i * 0.7) * jitter : 1;
        const sx = pos.x + x1 * pos.r * k * wob * scale;
        const sy = pos.y + y1 * pos.r * k * wob * scale;
        // Depth keys both brightness and size: that pairing is what makes a
        // flat scatter of squares read as a sphere.
        const depth = (z2 + 1) / 2;
        // The far side keeps a real floor rather than fading to almost
        // nothing: at 0.16 the back of the sphere was a sixth of an already
        // sub-1 alpha, which is what made her read as dim rather than distant.
        // The near/far ratio still carries the depth cue.
        const a = alpha * (0.3 + depth * 0.7);
        const px = 0.55 + depth * 1.05;
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
