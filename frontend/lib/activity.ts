// The Activity feed's filter, pulled out of the component so the panel's
// heading and the feed itself agree on the shown/total count without one
// having to call back into the other.
import type { DebugEvent } from "@/components/ActivityFeed";

export function filterEvents(
  events: DebugEvent[],
  filter: string,
  errorsOnly: boolean,
): DebugEvent[] {
  const needle = filter.trim().toLowerCase();
  return events.filter((ev) => {
    if (errorsOnly && ev.kind !== "error") return false;
    if (!needle) return true;
    // Everything the row shows is searchable — filtering on the summary alone
    // makes "voice" or a session id look like they match nothing.
    const hay = `${ev.source} ${ev.dest} ${ev.kind} ${ev.summary} ${ev.session || ""}`.toLowerCase();
    return hay.includes(needle);
  });
}
