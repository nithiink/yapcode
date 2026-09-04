"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

// Renders its children into document.body instead of wherever this component
// sits in the tree. Any true fullscreen overlay needs this: .vpanel is
// `position: absolute` with an explicit `z-index` (not `auto`), which makes it
// a stacking context of its own — every descendant, including a
// `position: fixed` one, then paints inside THAT context, capped at vpanel's
// z-index (4) relative to its siblings on the stage. .top (5), .orbhome/.hint
// (6) and .dock (7) all sit outside that context at a higher z-index, so an
// overlay nested under .vpanel can never paint above them no matter what
// z-index it gives itself — confirmed with document.elementFromPoint(),
// which kept returning .top-mid over a "fullscreen" overlay whose own
// getBoundingClientRect() already reported the full viewport. Portaling to
// body is the general fix: it removes the overlay from vpanel's subtree (and
// so its stacking context) entirely, and it also means no *future* transform/
// filter/perspective/will-change/contain added to vpanel or anything between
// it and body can turn into a `position: fixed` containing-block trap either.
// See docs/yuri/design/GUIDE.md's "position: fixed and stacking" section.
//
// SSR-safe: document doesn't exist on the server, so this renders nothing
// until it has mounted client-side (one extra paint, never visible as a
// flash since the overlay only opens in response to a client interaction).
export function Portal({ children }: { children: React.ReactNode }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);
  if (!mounted) return null;
  return createPortal(children, document.body);
}
