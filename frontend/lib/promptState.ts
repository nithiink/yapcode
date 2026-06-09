// Session-scoping for the single global "pending prompt" card.
//
// The UI shows at most one permission/question card at a time, but each card is
// owned by exactly one Claude session (its `sessionId`). Multiple sessions can
// be live at once, so a result that arrives for ONE session must never mutate
// another session's card. In particular: switching session B to auto mode
// auto-approves B's pending permission and drives B's turn to completion — and
// none of that may clear session A's still-pending prompt.

/** Anything carrying the owning session id; the real card carries more fields. */
export interface SessionScoped {
  sessionId: string;
}

/**
 * Next pending-card state after a result for `sessionId` would clear it.
 *
 * Returns `null` (clear) ONLY when there is a current card AND it belongs to
 * `sessionId`. A missing/empty `sessionId`, or a card owned by a different
 * session, leaves the current state untouched — so cross-session results can
 * never dismiss the wrong prompt.
 */
export function scopedClearPending<T extends SessionScoped>(
  current: T | null,
  sessionId: string | undefined | null,
): T | null {
  if (current && sessionId && current.sessionId === sessionId) return null;
  return current;
}
