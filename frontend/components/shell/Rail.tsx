"use client";

// The 56px icon rail. Replaces Phase 6's labelled nav: the labels moved into
// each panel's own heading, which is where the user is looking once a view is
// open, and the rail's job shrank to "which eight places are there".
//
// Every icon keeps an aria-label — an icon-only nav with no accessible name is
// eight identical buttons to a screen reader.
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useYuri } from "@/components/VoiceProvider";
import { navBadges } from "@/lib/dashboard.ts";

type Route = { href: string; label: string; badge?: "approvals" | "missions"; icon: React.ReactNode };

const ROUTES: Route[] = [
  { href: "/", label: "Dashboard", icon: <><circle cx="12" cy="12" r="9" /><path d="M12 3v18M3 12h18" /></> },
  { href: "/missions", label: "Missions", badge: "missions",
    icon: <><circle cx="6" cy="6" r="2.4" /><circle cx="18" cy="12" r="2.4" /><circle cx="6" cy="18" r="2.4" /><path d="M8.2 7.1l7.6 3.8M8.2 16.9l7.6-3.8" /></> },
  { href: "/projects", label: "Projects",
    icon: <path d="M3 6.5A1.5 1.5 0 014.5 5H9l2 2.5h8.5A1.5 1.5 0 0121 9v9a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 18z" /> },
  { href: "/agents", label: "Agents",
    icon: <><rect x="4" y="8" width="16" height="11" rx="2.5" /><path d="M12 4.5v3.5M8.5 13h.01M15.5 13h.01" /></> },
  { href: "/sessions", label: "Sessions",
    icon: <><rect x="3" y="5" width="18" height="14" rx="2.4" /><path d="M7 10l2.4 2.4L7 14.8M12 15h5" /></> },
  { href: "/approvals", label: "Approvals", badge: "approvals",
    icon: <><path d="M12 3l7.5 3.2v5.3c0 4.4-3 7.9-7.5 9.2-4.5-1.3-7.5-4.8-7.5-9.2V6.2z" /><path d="M9 12l2.2 2.2L15.4 10" /></> },
  { href: "/terminal", label: "Terminal",
    icon: <><rect x="3" y="4.5" width="18" height="15" rx="2.4" /><path d="M7.5 10.5l2.2 2.2-2.2 2.2M12.5 15h4" /></> },
  { href: "/activity", label: "Activity",
    icon: <path d="M3 13h3.6l2-5.5 3 11L17 6.5l1.7 6.5H21" /> },
];

export function Rail() {
  const { approvals, missions } = useYuri();
  const pathname = usePathname();
  const badges = navBadges(approvals, missions);

  return (
    <nav className="rail" aria-label="Views">
      <Link href="/" className="rail-mark" aria-label="Yuri — home">Y</Link>
      {ROUTES.map((r) => {
        const active = r.href === "/" ? pathname === "/" : pathname.startsWith(r.href);
        const count = r.badge ? badges[r.badge] : 0;
        return (
          <Link
            key={r.href}
            href={r.href}
            className="ric"
            title={r.label}
            aria-label={count > 0 ? `${r.label} (${count} waiting)` : r.label}
            aria-current={active ? "true" : undefined}
          >
            <svg viewBox="0 0 24 24">{r.icon}</svg>
            {count > 0 && <span className="ric-badge" aria-hidden="true">{count}</span>}
          </Link>
        );
      })}
    </nav>
  );
}
