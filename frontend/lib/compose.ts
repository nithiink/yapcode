// The dock composer's decision logic: may the typed text go anywhere right
// now, and if not, what does the box say instead?
//
// It lives here, out of the component, for one reason: this is the exact spot
// the original bug hid in. The composer used to accept text while pointing at
// a conversation nobody was looking at, so "nothing happened" had no visible
// cause. Every not-sendable case therefore has to carry its own words, and
// words are only trustworthy if they are tested — `node --test lib/*.test.ts`
// can reach a pure function, not a React tree.
import type { VoiceProvider, VoiceState } from "./voice.ts";
import { PROVIDER_LABEL } from "./voiceui.ts";

/** Whether this transport can accept a typed user turn at all.
 *
 *  Both shipping transports can: RealtimeSession pushes a `conversation.item.create`
 *  with role "user" and then asks for a response, and GeminiSession sends a
 *  `clientContent` turn with turnComplete. The predicate exists anyway because
 *  the honest failure for a future transport that cannot is a disabled box
 *  naming the provider — not a box that swallows what you typed. Flip an entry
 *  to false here and the composer explains itself without further changes. */
const CAN_TYPE: Record<VoiceProvider, boolean> = {
  azure: true,
  openai: true,
  gemini: true,
};

export function canTypeToProvider(provider: VoiceProvider): boolean {
  return CAN_TYPE[provider] ?? false;
}

export type ComposerInput = {
  provider: VoiceProvider;
  connected: boolean;
  vstate: VoiceState;
  /** A send is in flight (the transport call has not resolved). */
  sending: boolean;
  draft: string;
};

export type ComposerState = {
  /** Submitting right now would actually reach Yuri. */
  canSend: boolean;
  /** The text input itself is inert. */
  disabled: boolean;
  placeholder: string;
  /** Why it is inert, for a title/aria-description — null when it is usable. */
  reason: string | null;
};

/** What the composer can do, and what it says about it.
 *
 *  Order matters: a provider that cannot type is a permanent fact and outranks
 *  a connection state that will change on its own. */
export function composerState(input: ComposerInput): ComposerState {
  const { provider, connected, vstate, sending, draft } = input;

  if (!canTypeToProvider(provider)) {
    // `?? provider` so the reason names SOMETHING even for a provider added
    // to the union but not yet to the label map — "undefined voice can't…" is
    // the sort of half-honest message this whole module exists to prevent.
    const label = PROVIDER_LABEL[provider] ?? provider;
    const reason = `${label} voice can't take typed messages — speak to Yuri instead.`;
    return { canSend: false, disabled: true, placeholder: reason, reason };
  }
  if (!connected && vstate === "connecting") {
    const reason = "Connecting to Yuri…";
    return { canSend: false, disabled: true, placeholder: reason, reason };
  }
  if (!connected) {
    // Deliberately not "connect on demand": connecting grabs the microphone and
    // costs money per minute, and doing that behind a keystroke is a surprise.
    // The box says what to do and refuses the text until it can carry it.
    const reason = "Connect voice to type to Yuri";
    return { canSend: false, disabled: true, placeholder: reason, reason };
  }
  if (sending) {
    return { canSend: false, disabled: true, placeholder: "Sending…", reason: "Sending…" };
  }
  return {
    canSend: draft.trim().length > 0,
    disabled: false,
    placeholder: "Message Yuri…",
    reason: null,
  };
}
