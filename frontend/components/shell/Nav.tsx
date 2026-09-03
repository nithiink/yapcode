"use client";

// The eight-route nav. Lives in the layout beside the voice rail (see
// app/layout.tsx), so it never remounts on navigation. Badges come from
// navBadges(approvals, missions) — the same "what needs you" count the
// Dashboard's bands are built from, kept in sync because both read off the
// one useYuri() list rather than fetching their own copies.
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useYuri } from "@/components/VoiceProvider";
import { navBadges } from "@/lib/dashboard.ts";

const ROUTES: { href: string; label: string; badge?: "approvals" | "missions" }[] = [
  { href: "/", label: "Dashboard" },
  { href: "/missions", label: "Missions", badge: "missions" },
  { href: "/projects", label: "Projects" },
  { href: "/agents", label: "Agents" },
  { href: "/sessions", label: "Sessions" },
  { href: "/approvals", label: "Approvals", badge: "approvals" },
  { href: "/terminal", label: "Terminal" },
  { href: "/activity", label: "Activity" },
];

export function Nav() {
  const { approvals, missions } = useYuri();
  const pathname = usePathname();
  const badges = navBadges(approvals, missions);

  return (
    <nav className="shell-nav">
      <ul className="navlist">
        {ROUTES.map((r) => {
          const active = r.href === "/" ? pathname === "/" : pathname.startsWith(r.href);
          const count = r.badge ? badges[r.badge] : 0;
          return (
            <li key={r.href}>
              <Link href={r.href} className={`navitem ${active ? "active" : ""}`}>
                <span>{r.label}</span>
                {count > 0 && <span className="navbadge">{count}</span>}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
