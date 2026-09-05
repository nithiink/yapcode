// Voice-connection option maps and the tiny presentational helpers that turn
// provider/mode enums into copy — split out of the component but not tested:
// these are thin maps over realtime option types, and a test would pin wording
// rather than behaviour.
import type { RealtimeOptions, VoiceProvider, VoiceState } from "./voice.ts";
import type { NarrationMode } from "./narration.ts";

// Realtime models the user can pick per provider, best/most-capable first.
// Azure's list is dynamic: its "models" are the server-side deployment names
// (AZURE_OPENAI_DEPLOYMENTS env), fetched from /api/voice/models at mount —
// the static entry stays empty so no dropdown shows until they load.
export const MODEL_OPTIONS: Record<VoiceProvider, { value: string; label: string }[]> = {
  azure: [],
  openai: [
    { value: "gpt-realtime-2", label: "gpt-realtime-2 · most capable" },
    { value: "gpt-realtime-1.5", label: "gpt-realtime-1.5 · best audio" },
    { value: "gpt-realtime-mini", label: "gpt-realtime-mini · economy" },
  ],
  gemini: [
    { value: "gemini-3.1-flash-live-preview", label: "Gemini 3.1 Flash Live · best" },
    { value: "gemini-2.5-flash-native-audio-preview-12-2025", label: "Gemini 2.5 Native Audio" },
  ],
};

// Per-provider connection params. `model` is the user's dropdown choice.
export function connectionParams(provider: VoiceProvider, model: string): Partial<RealtimeOptions> {
  if (provider === "gemini") {
    return { provider: "gemini", model, voice: "Kore" };
  }
  if (provider === "azure") {
    // Azure-hosted OpenAI realtime. `model` is an Azure *deployment* name from
    // /api/voice/models; the backend only honors allowlisted names and falls
    // back to its default deployment otherwise (e.g. empty before the fetch).
    return { provider: "azure", model: model || undefined, voice: "marin" };
  }
  // OpenAI direct — the "native" option, kept switchable alongside Azure.
  return { provider: "openai", model, voice: "marin" };
}

// "azure" and "openai" are the same engine family reached over different
// routes (Azure-hosted deployment vs OpenAI direct) — the UI presents them as
// OpenAI with a route sub-choice, not as separate top-level providers.
export const PROVIDER_LABEL: Record<VoiceProvider, string> = {
  azure: "OpenAI · Azure",
  openai: "OpenAI",
  gemini: "Gemini",
};

// How much Yuri says out loud. Wording matches the voice instructions in
// lib/operating.ts so the button and the spoken command mean the same thing.
export const NARRATION_LABEL: Record<NarrationMode, { label: string; title: string }> = {
  quiet: { label: "Quiet", title: "Only problems and things needing your answer" },
  normal: { label: "Normal", title: "Meaningful progress (recommended)" },
  verbose: { label: "Verbose", title: "Every tool call and cost update too" },
};

// A calm, coarse caption for the orb. The orb's volume animation conveys the
// moment-to-moment activity, so we deliberately collapse listening/hearing/
// speaking into one steady "Listening" label instead of churning the words.
export function orbCaption(connected: boolean, muted: boolean, vstate: VoiceState): string {
  if (!connected || vstate === "idle") return "Offline";
  if (muted) return "Muted";
  if (vstate === "connecting") return "Connecting…";
  if (vstate === "thinking") return "Thinking…"; // keep this one — it's a genuine longer pause
  return "Listening";
}
