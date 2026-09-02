// Yuri's spoken lines are authored by the backend (see
// docs/superpowers/specs/2026-09-02-yuri-orchestration-narration-design.md §4),
// which attaches a `narration` field to both the poll result and each SSE
// frame. The frontend's entire rule is: if it has a line, inject it.
//
// Keeping that rule here — rather than inline in VoiceAgent — makes it
// testable and keeps the "who phrases it" boundary obvious.

export type NarrationMode = "quiet" | "normal" | "verbose";
export const NARRATION_MODES: NarrationMode[] = ["quiet", "normal", "verbose"];

/** Narrow whatever GET/PUT /yuri/narration returned. The mode drives a control
 *  and is echoed back over the network, so it is validated, not asserted. */
export function isNarrationMode(x: unknown): x is NarrationMode {
  return typeof x === "string" && (NARRATION_MODES as string[]).includes(x);
}

export type NarratedFrame = { narration?: string | null };

/** The line to speak, or null. Non-strings and blanks are ignored — the field
 *  crosses a network boundary, so it is not trusted to be well-formed. The
 *  parameter is `unknown` because callers hand it raw JSON.parse output. */
export function narrationOf(x: unknown): string | null {
  if (!x || typeof x !== "object") return null;
  const n = (x as NarratedFrame).narration;
  if (typeof n !== "string") return null;
  const trimmed = n.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** A frame off /yuri/events/stream: a serialized YuriEvent plus the line. */
export type NarratedEvent = NarratedFrame & { id?: unknown };

/**
 * How many events the narration stream replays, and how many ids are seeded
 * into the gate before it opens.
 *
 * ONE constant for BOTH numbers, deliberately. The seed is what makes the
 * replay silent, so the two must always match: raise the stream's limit
 * without the seed's and every replayed-but-unseeded event is spoken as news
 * at connect — the exact failure the gate exists to prevent. Callers must use
 * this for both `/yuri/events?limit=` and `/yuri/events/stream?limit=`.
 *
 * 50 rather than 1: the stream has no Last-Event-ID resume (`_frame` emits no
 * `id:` line), so after an EventSource blip everything that happened in the
 * gap would otherwise never be narrated. A replay wide enough to cover a
 * typical blip closes that gap, and the gate dedupes the overlap for free.
 * Well inside the backend's own bounds — `_clamp_limit` is
 * `max(1, min(limit, 1000))` — and comfortably under the gate's cap, so the
 * replayed ids can never be evicted before they are replayed.
 */
export const NARRATION_REPLAY_LIMIT = 50;

export type SpokenGate = {
  /** Mark ids as already delivered without speaking them. */
  seed(ids: Iterable<unknown>): void;
  /** The line to speak for this frame, or null if blank or already delivered. */
  lineFor(frame: unknown): string | null;
};

/**
 * Dedupe by event id, so the narration stream can never re-speak history.
 *
 * This is load-bearing, not belt-and-braces. `GET /yuri/events/stream` replays
 * its newest `limit` events to every new connection, and the backend clamps
 * `limit` with `max(1, …)` (yuri/api/routes.py `_clamp_limit`) — so `limit=0`
 * does NOT switch the replay off, and EventSource reconnects on its own after
 * any blip. The backend also subscribes to the bus *before* it replays, so one
 * event can legitimately arrive twice on a single connection.
 *
 * The gate tracks delivery rather than speech: an event that arrived with
 * `narration: null` (quiet mode) is still remembered, so a mode change plus a
 * reconnect cannot resurface it as news.
 *
 * A frame with no usable id fails OPEN — better a rare repeat than a silently
 * swallowed line the user needed to hear.
 */
export function createSpokenGate(cap = 500): SpokenGate {
  const seen = new Set<string>();
  const order: string[] = [];

  const remember = (id: string) => {
    if (seen.has(id)) return;
    seen.add(id);
    order.push(id);
    // Evict oldest-first. The replayed frame is always the store's newest
    // event, so eviction can only ever touch ids older than any replay.
    while (order.length > cap) {
      const gone = order.shift();
      if (gone !== undefined) seen.delete(gone);
    }
  };

  const idOf = (frame: unknown): string | null => {
    if (!frame || typeof frame !== "object") return null;
    const id = (frame as NarratedEvent).id;
    return typeof id === "string" && id.length > 0 ? id : null;
  };

  return {
    seed(ids) {
      for (const id of ids) if (typeof id === "string" && id.length > 0) remember(id);
    },
    lineFor(frame) {
      const id = idOf(frame);
      if (id !== null) {
        if (seen.has(id)) return null;
        remember(id);
      }
      return narrationOf(frame);
    },
  };
}
