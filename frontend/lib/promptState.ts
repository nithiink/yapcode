export interface SessionScoped {
  sessionId: string;
}

// Clear the single global prompt card only when it belongs to `sessionId`; a
// result for any other session (or a missing id) leaves it untouched, so one
// session's activity can never dismiss another's pending prompt.
export function scopedClearPending<T extends SessionScoped>(
  current: T | null,
  sessionId: string | undefined | null,
): T | null {
  if (current && sessionId && current.sessionId === sessionId) return null;
  return current;
}
