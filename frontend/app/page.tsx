// Home is the closed state of the panel, not a view: the stage renders Yuri
// centred with her one status line (components/shell/Home.tsx), and the dock
// carries anything that needs a decision. So this route deliberately renders
// nothing into the panel.
//
// This is what became of Phase 6's Dashboard. Its three bands of cards are not
// lost — the triage that ordered them (lib/dashboard.ts's bands()) now drives
// the line under her name (lib/presence.ts), pending approvals arrive in the
// dock's prompt card, and the full lists are one rail click away in
// /approvals, /missions and /sessions. See docs/yuri/design/README.md.
export default function Page() {
  return null;
}
