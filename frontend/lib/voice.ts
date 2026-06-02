// Shared types + cost model for the voice providers.
//
// Two transports implement the same VoiceSession interface so the UI is
// provider-agnostic:
//   - RealtimeSession (lib/realtime.ts) — OpenAI / Azure OpenAI over WebRTC
//   - GeminiSession   (lib/gemini.ts)   — Google Gemini Live over WebSocket

export type ToolDef = {
  type: "function";
  name: string;
  description?: string;
  parameters?: Record<string, unknown>;
};

export type VoiceState =
  | "idle"
  | "connecting"
  | "listening"
  | "hearing"
  | "thinking"
  | "speaking";

export type VoiceUsage = {
  audioInTokens: number;
  audioCachedTokens: number;
  textInTokens: number;
  textCachedTokens: number;
  audioOutTokens: number;
  textOutTokens: number;
  costUsd: number; // cumulative voice cost for the connection
  cacheHitRate: number; // cached audio-in / total audio-in, 0..1
};

export type RealtimeEvent =
  | { type: "status"; status: string }
  | { type: "state"; state: VoiceState }
  | { type: "transcript"; role: "user" | "assistant"; text: string; final: boolean }
  | { type: "tool_call"; name: string; arguments?: unknown; result?: unknown; ok?: boolean }
  | { type: "usage"; usage: VoiceUsage }
  | { type: "error"; message: string };

// UI-level provider choice. "azure" and "openai" are both the OpenAI realtime
// WebRTC family — azure routes through your Azure OpenAI deployment, openai is
// the direct OpenAI API ("native").
export type VoiceProvider = "azure" | "openai" | "gemini";

export type ClaudeBackend = "cli" | "sdk";

export type RealtimeOptions = {
  provider?: string; // sent to /session; undefined => backend VOICE_PROVIDER default
  model?: string; // undefined => backend default for the provider
  voice?: string;
  instructions: string;
  backend?: ClaudeBackend; // injected into start_session tool calls
  onEvent: (e: RealtimeEvent) => void;
  onRemoteStream?: (stream: MediaStream) => void; // drives the orb analyser
  onLocalStream?: (stream: MediaStream) => void; // drives the orb analyser from the user's mic
  onDebug?: (msg: string) => void; // low-level transport trace (tool-call lifecycle)
};

export interface VoiceSession {
  activeModel?: string; // actual model/deployment reported by the backend
  start(audioEl: HTMLAudioElement): Promise<void>;
  stop(): void;
  // Inject an out-of-band update (e.g. a background Claude result) and prompt
  // the model to speak about it, even mid-conversation.
  injectUpdate(text: string): void;
  // Mute/unmute the microphone (the agent stops hearing the user).
  setMuted(muted: boolean): void;
}

// --- cost model -----------------------------------------------------------
// USD per 1M tokens. Picked by substring match on the active model name.
export type Rates = {
  textIn: number;
  textCached: number;
  audioIn: number;
  audioCached: number;
  textOut: number;
  audioOut: number;
};

const OPENAI_MINI_RATES: Rates = {
  textIn: 0.6, textCached: 0.06, audioIn: 10, audioCached: 0.3, textOut: 2.4, audioOut: 20,
};

const RATE_TABLE: Array<{ match: RegExp; rates: Rates }> = [
  // Gemini Live (no separate cached-audio tier published — reuse audio rate).
  { match: /gemini.*native-audio/i, rates: { textIn: 0.5, textCached: 0.5, audioIn: 3, audioCached: 3, textOut: 2, audioOut: 12 } },
  { match: /gemini.*live/i, rates: { textIn: 0.75, textCached: 0.75, audioIn: 3, audioCached: 3, textOut: 4.5, audioOut: 12 } },
  { match: /gemini/i, rates: { textIn: 0.5, textCached: 0.5, audioIn: 3, audioCached: 3, textOut: 2, audioOut: 12 } },
  // OpenAI / Azure realtime.
  { match: /mini/i, rates: OPENAI_MINI_RATES },
  { match: /4o-realtime/i, rates: { textIn: 5, textCached: 2.5, audioIn: 40, audioCached: 2.5, textOut: 20, audioOut: 80 } },
  { match: /realtime/i, rates: { textIn: 4, textCached: 0.4, audioIn: 32, audioCached: 0.4, textOut: 16, audioOut: 64 } },
];

export function ratesFor(model?: string): Rates {
  if (model) for (const { match, rates } of RATE_TABLE) if (match.test(model)) return rates;
  return OPENAI_MINI_RATES;
}

export function emptyUsage(): VoiceUsage {
  return {
    audioInTokens: 0, audioCachedTokens: 0, textInTokens: 0,
    textCachedTokens: 0, audioOutTokens: 0, textOutTokens: 0,
    costUsd: 0, cacheHitRate: 0,
  };
}

export function recomputeCost(acc: VoiceUsage, model?: string): void {
  const r = ratesFor(model);
  const m = 1_000_000;
  acc.costUsd =
    (acc.audioCachedTokens * r.audioCached +
      (acc.audioInTokens - acc.audioCachedTokens) * r.audioIn +
      acc.textCachedTokens * r.textCached +
      (acc.textInTokens - acc.textCachedTokens) * r.textIn +
      acc.audioOutTokens * r.audioOut +
      acc.textOutTokens * r.textOut) /
    m;
  acc.cacheHitRate = acc.audioInTokens > 0 ? acc.audioCachedTokens / acc.audioInTokens : 0;
}
