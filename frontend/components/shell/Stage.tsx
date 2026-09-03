"use client";

// The stage: everything to the right of the rail. It owns the one piece of
// state the whole shell turns on — whether Yuri has yielded the stage.
//
// "Engaged" is derived from the route, not stored: a panel is open iff the
// path is not "/", so a deep link, a reload and the back button all land in
// the right state without a store to keep in sync. The one thing the route
// cannot tell us is the composer or a session tab taking focus with no panel
// open, which is what `touched` adds — and any navigation clears it, since the
// route then answers on its own.
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Orb } from "./Orb";
import { TopBar } from "./TopBar";
import { Home } from "./Home";
import { Dock } from "./Dock";

export function Stage({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const panelOpen = pathname !== "/";
  const [touched, setTouched] = useState(false);

  // A route change re-derives engagement, so a stale `touched` must not keep
  // her in the corner after the user goes home.
  useEffect(() => setTouched(false), [pathname]);

  const engaged = panelOpen || touched;

  const goHome = () => {
    setTouched(false);
    router.push("/");
  };

  return (
    <main className="stage">
      <Orb engaged={engaged} />
      <TopBar />

      {/* The panel is always mounted so its open/close transition can run;
          `children` is a route, and Next has already swapped it by the time
          this renders. Rendering the wrapper only when open would make the
          panel appear with no transition and vanish with none. */}
      <section className="vpanel" data-open={panelOpen} aria-hidden={!panelOpen}>
        <button className="vpanel-close" onClick={goHome} aria-label="Close and return to Yuri">✕</button>
        {children}
      </section>

      <Home away={engaged} />

      {/* The orb itself is a canvas with pointer-events off, so returning home
          by clicking her needs a real control over where she sits. It is only
          hittable while she is in the corner. */}
      <button
        className="orbhome"
        data-shown={engaged}
        onClick={goHome}
        aria-label="Back to Yuri"
        tabIndex={engaged ? 0 : -1}
      >
        <span>Home</span>
      </button>

      <Dock onEngage={() => setTouched(true)} />
    </main>
  );
}
