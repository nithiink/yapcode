// Who gets to speak next.
//
// Yuri has one mouth and several things competing for it: a turn she is
// already mid-way through, a background result that just landed, a permission
// request that needs an answer. On the Gemini transport, "add this to the
// conversation" and "now respond to it" are the SAME message — a complete user
// turn — and a new turn preempts whatever the model is currently saying. So a
// build finishing mid-sentence used to cut her off in the middle of a word.
//
// (The OpenAI transport does not have this problem: there the two are separate
// messages, so realtime.ts adds the item silently and defers only the
// response.create. This module exists for the transports where they are one.)
//
// Pure, because the interesting behaviour is a state machine and `node --test`
// is the only test runner this repo has. The transport owns the socket; this
// owns the decision.
import { enqueueInjection, MAX_PENDING_INJECTIONS, type PendingInjection } from "./narration.ts";

export type SpeechQueue = {
  /** True while a turn is in flight — hers or one she is answering. */
  responding: boolean;
  pending: PendingInjection[];
};

export const newSpeechQueue = (): SpeechQueue => ({ responding: false, pending: [] });

/** What the transport should do about an update it was handed. */
export type SubmitResult = {
  /** Send this on the wire now, or null to hold. */
  send: string | null;
  /** Texture lines evicted by the bound, for logging. Never a blocking one. */
  dropped: number;
};

export function submit(q: SpeechQueue, text: string, blocking = false,
                       cap = MAX_PENDING_INJECTIONS): SubmitResult {
  if (!q.responding) {
    q.responding = true;
    return { send: text, dropped: 0 };
  }
  // A blocking line (permission / question) can never be evicted by the bound
  // — the frontend half of the backend's ALWAYS_SPEAK guarantee. A dropped ask
  // is unrecoverable: poll_status hands each buffered result back exactly once.
  const dropped = enqueueInjection(q.pending, { text, blocking }, cap);
  return { send: null, dropped };
}

/** A turn began — hers, or one she is answering. Nothing may preempt it. */
export function noteTurnStart(q: SpeechQueue): void {
  q.responding = true;
}

/** A turn closed. Releases exactly ONE held update, in arrival order, so each
 *  still gets its own spoken response rather than being merged into one. */
export function noteTurnComplete(q: SpeechQueue): string | null {
  q.responding = false;
  const next = q.pending.shift();
  if (next) q.responding = true;
  return next?.text ?? null;
}

/** The user cut in.
 *
 *  Deliberately releases nothing: they wanted the floor, and pushing a queued
 *  update at them now is the same rudeness this queue exists to prevent, just
 *  aimed the other way. The backlog waits for the next turn to close, or for
 *  the force-drain. */
export function noteInterrupted(q: SpeechQueue): void {
  q.responding = false;
}

/** The escape hatch: a turn that never closed.
 *
 *  A queue that can deadlock is worse than one that eventually gives up — a
 *  stalled session would otherwise swallow every background result silently.
 *  Releases one and leaves the rest queued. */
export function forceDrain(q: SpeechQueue): string | null {
  q.responding = false;
  return noteTurnComplete(q);
}

/** True when something is waiting, so the transport knows to keep a timer armed. */
export const hasPending = (q: SpeechQueue): boolean => q.pending.length > 0;

/** Dropped on teardown: a pending release must not fire into a dead socket. */
export function reset(q: SpeechQueue): void {
  q.responding = false;
  q.pending.length = 0;
}
