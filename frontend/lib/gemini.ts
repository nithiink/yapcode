// Browser <-> Google Gemini Live over WebSocket.
//
// Flow:
//  1. POST /api/session {provider:"gemini"} -> backend mints a single-use
//     ephemeral token (data.value) and returns ws_url + model + voice.
//  2. Open a WebSocket to `${ws_url}?access_token=<token>` (the v1alpha
//     BidiGenerateContentConstrained endpoint — browsers can't set headers, so
//     the token rides in the query string).
//  3. Send the `setup` message; wait for `setupComplete`.
//  4. Capture mic at 16 kHz, convert to 16-bit PCM, base64, stream as
//     realtimeInput.audio (server VAD handles turn-taking).
//  5. Play 24 kHz PCM from serverContent.modelTurn.parts[].inlineData.
//  6. On toolCall, POST to /api/tools/execute and reply with toolResponse.
//
// Unlike OpenAI's WebRTC transport there's no MediaStream from the network, so
// playback is rendered through Web Audio and tapped into a MediaStream that we
// hand to onRemoteStream — the orb analyser then works unchanged.

import {
  RealtimeEvent,
  RealtimeOptions,
  ToolDef,
  VoiceSession,
  VoiceUsage,
  emptyUsage,
  recomputeCost,
} from "./voice";
import { authHeaders } from "./auth";

const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;

// If nothing has been sent on the socket for this long (e.g. the mic is muted,
// or the user is silently waiting on a long Claude turn), send a short burst of
// silence so the server doesn't treat the stream as idle and drop the session.
const KEEPALIVE_MS = 10000;

// AudioWorklet that converts mic Float32 frames to 16-bit PCM and reports RMS
// (used to drive the "hearing" orb state). Loaded via a Blob URL so we don't
// ship a separate static asset.
const CAPTURE_WORKLET = `
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      const pcm = new Int16Array(ch.length);
      let sum = 0;
      for (let i = 0; i < ch.length; i++) {
        let s = Math.max(-1, Math.min(1, ch[i]));
        sum += s * s;
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(
        { pcm: pcm.buffer, rms: Math.sqrt(sum / ch.length) },
        [pcm.buffer],
      );
    }
    return true;
  }
}
registerProcessor('capture-processor', CaptureProcessor);
`;

function b64encode(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + 0x8000)));
  }
  return btoa(s);
}

function b64decode(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

// Gemini's setup is parsed as protobuf-JSON, where Schema.type is an uppercase
// enum (OBJECT/STRING/...). Our backend emits lowercase JSON-Schema, so convert
// recursively and drop keywords Gemini's Schema subset doesn't accept.
const GEMINI_TYPE: Record<string, string> = {
  object: "OBJECT", string: "STRING", number: "NUMBER",
  integer: "INTEGER", boolean: "BOOLEAN", array: "ARRAY",
};

function convertSchema(s: any): any {
  if (!s || typeof s !== "object") return s;
  const out: any = {};
  if (s.type) out.type = GEMINI_TYPE[String(s.type).toLowerCase()] || String(s.type).toUpperCase();
  if (s.description) out.description = s.description;
  if (Array.isArray(s.enum)) out.enum = s.enum;
  if (s.properties && typeof s.properties === "object") {
    out.properties = {};
    for (const k of Object.keys(s.properties)) out.properties[k] = convertSchema(s.properties[k]);
  }
  if (Array.isArray(s.required) && s.required.length) out.required = s.required;
  if (s.items) out.items = convertSchema(s.items);
  return out;
}

// OpenAI-style tool defs -> Gemini functionDeclarations. No-arg tools omit
// `parameters` entirely — Gemini rejects an OBJECT schema with empty properties.
function toGeminiTools(tools: ToolDef[]): unknown[] {
  if (!tools.length) return [];
  return [
    {
      functionDeclarations: tools.map((t) => {
        const decl: Record<string, unknown> = { name: t.name, description: t.description };
        const params = t.parameters as any;
        if (params?.properties && Object.keys(params.properties).length > 0) {
          decl.parameters = convertSchema(params);
        }
        return decl;
      }),
    },
  ];
}

export class GeminiSession implements VoiceSession {
  private ws?: WebSocket;
  private opts: RealtimeOptions;
  activeModel?: string;

  private inCtx?: AudioContext; // 16 kHz capture
  private outCtx?: AudioContext; // 24 kHz playback
  private micStream?: MediaStream;
  private worklet?: AudioWorkletNode;
  private playDest?: MediaStreamAudioDestinationNode;
  private playCursor = 0; // next scheduled playback time
  private liveSources = new Set<AudioBufferSourceNode>();

  private tools: ToolDef[] = [];
  private setupDone = false;
  private muted = false;
  private assistantText = "";
  private userText = "";
  private usage: VoiceUsage = emptyUsage();

  // Reconnection: Gemini ends a session on a time/context limit. We keep the
  // latest session-resumption handle and, on an unexpected close, reconnect with
  // it to resume the SAME conversation (context preserved).
  private stopped = false;          // set by stop() so we don't reconnect on purpose
  private reconnecting = false;
  private resumeHandle?: string;    // latest handle from sessionResumptionUpdate
  private connectVoice?: string;
  private onSetupComplete?: () => void;
  private keepaliveTimer?: ReturnType<typeof setInterval>;
  private lastSend = 0;             // ms timestamp of the last frame sent
  // An update that arrived while the socket was down (latest wins). Flushed on
  // reconnect so a Claude result completed during an outage isn't lost.
  private bufferedInject?: string;

  constructor(opts: RealtimeOptions) {
    this.opts = opts;
  }

  async start(_audioEl: HTMLAudioElement): Promise<void> {
    const emit = this.opts.onEvent;
    emit({ type: "status", status: "Minting token..." });

    const toolsRes = await fetch("/api/tools", { headers: authHeaders() });
    if (toolsRes.ok) this.tools = (await toolsRes.json()).tools || [];

    // Prepare audio graph before the socket opens so we can stream immediately.
    // It persists across reconnects — only the WebSocket is re-established.
    await this.initAudio();
    await this.openSocket();
  }

  // Mint a fresh single-use token and open the Gemini WebSocket. Pass a
  // resumption handle to resume an existing conversation (used on reconnect).
  private async openSocket(resumeHandle?: string): Promise<void> {
    const emit = this.opts.onEvent;
    const sessionRes = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        provider: "gemini",
        model: this.opts.model,
        voice: this.opts.voice,
      }),
    });
    if (!sessionRes.ok) throw new Error(`Session error: ${await sessionRes.text()}`);
    const session = await sessionRes.json();
    const token: string = session.value;
    const wsUrl: string = session.ws_url;
    if (!token || !wsUrl) throw new Error("Gemini session response missing token/ws_url");
    this.activeModel = session.model;
    this.connectVoice = session.voice || this.opts.voice || "Kore";

    emit({ type: "status", status: resumeHandle ? "Reconnecting to Gemini..." : "Connecting to Gemini..." });
    const ws = new WebSocket(`${wsUrl}?access_token=${encodeURIComponent(token)}`);
    this.ws = ws;

    ws.addEventListener("open", () => this.sendSetup(session.model, this.connectVoice!, resumeHandle));
    ws.addEventListener("message", (ev) => this.onMessage(ev.data));
    ws.addEventListener("error", () => {
      if (!this.reconnecting) emit({ type: "error", message: "Gemini WebSocket error" });
    });
    ws.addEventListener("close", (ev) => this.onClose(ev));
  }

  stop(): void {
    this.stopped = true;  // user-initiated: the close handler must NOT reconnect
    this.stopKeepalive();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = undefined;
    this.worklet?.disconnect();
    this.micStream?.getTracks().forEach((t) => t.stop());
    this.stopAllPlayback();
    this.inCtx?.close().catch(() => undefined);
    this.outCtx?.close().catch(() => undefined);
    this.inCtx = undefined;
    this.outCtx = undefined;
    this.setupDone = false;
    this.opts.onEvent({ type: "status", status: "Disconnected." });
  }

  // --- reconnection -------------------------------------------------------

  private onClose(ev: CloseEvent): void {
    // Intentional disconnect, or a reconnect attempt already in flight: ignore.
    if (this.stopped || this.reconnecting) return;
    this.stopKeepalive();
    this.setupDone = false;
    this.opts.onEvent({ type: "status", status: `Connection lost (${ev.code}) — reconnecting…` });
    this.opts.onEvent({ type: "state", state: "connecting" });
    void this.reconnectLoop();
  }

  // Resolves once the next setupComplete arrives, or false on timeout.
  private waitForSetup(timeoutMs: number): Promise<boolean> {
    return new Promise((resolve) => {
      let done = false;
      const finish = (ok: boolean) => {
        if (done) return;
        done = true;
        this.onSetupComplete = undefined;
        resolve(ok);
      };
      this.onSetupComplete = () => finish(true);
      setTimeout(() => finish(false), timeoutMs);
    });
  }

  private async reconnectLoop(): Promise<void> {
    if (this.reconnecting) return;
    this.reconnecting = true;
    const backoff = [300, 1000, 2000, 4000, 8000, 8000];
    for (let i = 0; i < backoff.length && !this.stopped; i++) {
      await new Promise((r) => setTimeout(r, backoff[i]));
      if (this.stopped) break;
      try {
        await this.openSocket(this.resumeHandle);
      } catch {
        continue; // mint/open failed — back off and retry
      }
      if (await this.waitForSetup(8000)) {
        this.reconnecting = false;
        this.notifyReconnected();
        return;
      }
      try { this.ws?.close(); } catch { /* ignore */ }  // setup stalled — retry
    }
    this.reconnecting = false;
    if (!this.stopped) {
      this.opts.onEvent({
        type: "error",
        message: "Couldn't reconnect the voice session. Please disconnect and reconnect.",
      });
    }
  }

  private notifyReconnected(): void {
    this.opts.onEvent({ type: "status", status: "Reconnected." });
    this.opts.onEvent({ type: "state", state: "listening" });
    const buffered = this.bufferedInject;
    this.bufferedInject = undefined;
    if (buffered) {
      // A narration update landed while the socket was down. Deliver the freshest
      // one now so the user hears the real current state — not a stale echo.
      this.injectUpdate(
        "[connection] You briefly lost the voice connection and just reconnected. " +
          "Tell the user in a few words that you're back, then relay this update:\n" +
          buffered,
      );
    } else {
      // Nothing happened during the gap. Announce the reconnect, but do NOT
      // re-narrate the prior turn — replaying a now-stale question (sometimes
      // minutes old) just confuses the user.
      this.injectUpdate(
        "[connection] You briefly lost the voice connection and just reconnected. In one " +
          "short sentence let the user know you're back, then wait for them — do NOT repeat " +
          "your previous question or message unless they ask.",
      );
    }
  }

  // --- audio setup --------------------------------------------------------
  private async initAudio() {
    this.micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.opts.onLocalStream?.(this.micStream); // feed the user's mic to the orb analyser

    this.inCtx = new AudioContext({ sampleRate: INPUT_RATE });
    const blobUrl = URL.createObjectURL(
      new Blob([CAPTURE_WORKLET], { type: "application/javascript" }),
    );
    await this.inCtx.audioWorklet.addModule(blobUrl);
    URL.revokeObjectURL(blobUrl);
    const src = this.inCtx.createMediaStreamSource(this.micStream);
    this.worklet = new AudioWorkletNode(this.inCtx, "capture-processor");
    this.worklet.port.onmessage = (e) => this.onMicChunk(e.data);
    src.connect(this.worklet);
    // Worklet must be in the graph to pull; route to a muted gain (no output).
    const sink = this.inCtx.createGain();
    sink.gain.value = 0;
    this.worklet.connect(sink).connect(this.inCtx.destination);

    this.outCtx = new AudioContext({ sampleRate: OUTPUT_RATE });
    // Browsers may start the context suspended until a user gesture; connect()
    // runs from the click handler, so resume is allowed here.
    await this.outCtx.resume().catch(() => undefined);
    this.playDest = this.outCtx.createMediaStreamDestination();
    // Tap playback into a MediaStream so the orb analyser reacts to the voice.
    this.opts.onRemoteStream?.(this.playDest.stream);
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.micStream?.getAudioTracks().forEach((t) => (t.enabled = !muted));
  }

  private onMicChunk(data: { pcm: ArrayBuffer; rms: number }) {
    if (this.muted || !this.setupDone || this.ws?.readyState !== WebSocket.OPEN) return;
    this.opts.onEvent({ type: "state", state: data.rms > 0.02 ? "hearing" : "listening" });
    this.ws.send(
      JSON.stringify({
        realtimeInput: {
          audio: { data: b64encode(data.pcm), mimeType: `audio/pcm;rate=${INPUT_RATE}` },
        },
      }),
    );
    this.lastSend = Date.now();
  }

  injectUpdate(text: string): void {
    // Socket down (e.g. mid-reconnect): hold the latest update and flush it once
    // we're back, so a result that completed during the outage isn't dropped.
    if (this.ws?.readyState !== WebSocket.OPEN) {
      this.bufferedInject = text;
      return;
    }
    // A complete user turn nudges the model to respond about the update.
    this.ws.send(
      JSON.stringify({
        clientContent: {
          turns: [{ role: "user", parts: [{ text }] }],
          turnComplete: true,
        },
      }),
    );
    this.lastSend = Date.now();
  }

  // --- keepalive ----------------------------------------------------------
  private startKeepalive(): void {
    this.stopKeepalive();
    this.lastSend = Date.now();
    this.keepaliveTimer = setInterval(() => {
      if (this.ws?.readyState !== WebSocket.OPEN || !this.setupDone) return;
      if (Date.now() - this.lastSend < KEEPALIVE_MS) return;
      // 100 ms of zeroes — pure silence, so server VAD won't read it as a turn,
      // but it keeps the audio stream from idling out.
      const silence = new Int16Array(INPUT_RATE / 10);
      this.ws.send(
        JSON.stringify({
          realtimeInput: {
            audio: { data: b64encode(silence.buffer), mimeType: `audio/pcm;rate=${INPUT_RATE}` },
          },
        }),
      );
      this.lastSend = Date.now();
    }, KEEPALIVE_MS);
  }

  private stopKeepalive(): void {
    if (this.keepaliveTimer) {
      clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = undefined;
    }
  }

  // --- protocol -----------------------------------------------------------
  private sendSetup(model: string, voice: string, resumeHandle?: string) {
    const setup: Record<string, unknown> = {
      model: model.startsWith("models/") ? model : `models/${model}`,
      generationConfig: {
        responseModalities: ["AUDIO"],
        speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice } } },
      },
      systemInstruction: { parts: [{ text: this.opts.instructions }] },
      tools: toGeminiTools(this.tools),
      inputAudioTranscription: {},
      outputAudioTranscription: {},
      // Resume the prior conversation when reconnecting (handle preserves
      // context); otherwise start a fresh resumable session.
      sessionResumption: resumeHandle ? { handle: resumeHandle } : {},
      // Let a single session outlive the model's context window by compressing
      // the oldest turns — so long conversations don't hit a hard context cap.
      contextWindowCompression: { slidingWindow: {} },
    };
    const fnCount = (toGeminiTools(this.tools)[0] as any)?.functionDeclarations?.length || 0;
    console.log(`[gemini] setup model=${setup.model} tools=${fnCount}`);
    this.ws?.send(JSON.stringify({ setup }));
    this.opts.onEvent({ type: "status", status: "Setting up..." });
  }

  private async onMessage(data: any) {
    let text: string;
    if (typeof data === "string") text = data;
    else if (data instanceof Blob) text = await data.text();
    else if (data instanceof ArrayBuffer) text = new TextDecoder().decode(data);
    else return;

    let msg: any;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    const emit = this.opts.onEvent;

    if (msg.setupComplete) {
      this.setupDone = true;
      this.startKeepalive();
      this.onSetupComplete?.();  // unblocks a reconnect attempt waiting on setup
      emit({ type: "status", status: "Connected — start talking." });
      emit({ type: "state", state: "listening" });
      return;
    }

    // Gemini periodically emits a resumption handle; keep the latest so we can
    // resume this exact conversation if the socket drops.
    if (msg.sessionResumptionUpdate) {
      const u = msg.sessionResumptionUpdate;
      if (u.resumable && u.newHandle) this.resumeHandle = u.newHandle;
      return;
    }

    // Sent shortly before Gemini terminates a session (e.g. time limit). The
    // socket will close next; the close handler reconnects with the handle.
    if (msg.goAway) {
      console.log("[gemini] goAway, timeLeft=", msg.goAway.timeLeft);
      emit({ type: "status", status: "Session expiring — will reconnect…" });
      return;
    }

    if (msg.serverContent) {
      const sc = msg.serverContent;
      if (sc.interrupted) {
        this.stopAllPlayback();
        emit({ type: "state", state: "listening" });
      }
      if (sc.inputTranscription?.text) {
        this.userText += sc.inputTranscription.text;
        emit({ type: "transcript", role: "user", text: this.userText, final: false });
      }
      if (sc.outputTranscription?.text) {
        this.assistantText += sc.outputTranscription.text;
        emit({ type: "transcript", role: "assistant", text: this.assistantText, final: false });
      }
      const parts = sc.modelTurn?.parts || [];
      for (const p of parts) {
        if (p.inlineData?.data) {
          emit({ type: "state", state: "speaking" });
          this.playPcm(p.inlineData.data);
        }
      }
      if (sc.turnComplete) {
        if (this.userText) {
          emit({ type: "transcript", role: "user", text: this.userText, final: true });
          this.userText = "";
        }
        if (this.assistantText) {
          emit({ type: "transcript", role: "assistant", text: this.assistantText, final: true });
          this.assistantText = "";
        }
        emit({ type: "state", state: "listening" });
      }
    }

    if (msg.toolCall?.functionCalls) {
      emit({ type: "state", state: "thinking" });
      for (const fc of msg.toolCall.functionCalls) await this.runFunctionCall(fc);
    }

    if (msg.toolCallCancellation) {
      console.log("[gemini] toolCallCancellation", msg.toolCallCancellation.ids);
    }

    if (msg.usageMetadata) this.accumulateUsage(msg.usageMetadata);

    // Surface anything unexpected (error frames, goAway, etc.) for debugging.
    if (!msg.setupComplete && !msg.serverContent && !msg.toolCall && !msg.usageMetadata) {
      console.log("[gemini] message:", Object.keys(msg), msg);
      if (msg.error) emit({ type: "error", message: msg.error.message || JSON.stringify(msg.error) });
    }
  }

  private async runFunctionCall(fc: { id?: string; name: string; args?: any }) {
    const emit = this.opts.onEvent;
    const args = fc.args || {};
    // The app, not the model, chooses the execution backend.
    if (fc.name === "start_session" && this.opts.backend) args.backend = this.opts.backend;
    emit({ type: "tool_call", name: fc.name, arguments: args });

    let result: unknown;
    let ok = true;
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: fc.name, arguments: args }),
      });
      const out = await r.json();
      result = out.result ?? out;
      ok = !!out.ok;
    } catch (e: any) {
      ok = false;
      result = { error: e?.message || String(e) };
    }
    emit({ type: "tool_call", name: fc.name, arguments: args, result, ok });

    this.ws?.send(
      JSON.stringify({
        toolResponse: {
          functionResponses: [{ id: fc.id, name: fc.name, response: { result } }],
        },
      }),
    );
  }

  // --- playback -----------------------------------------------------------
  private playPcm(b64: string) {
    if (!this.outCtx || !this.playDest) return;
    const pcm = new Int16Array(b64decode(b64));
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 0x8000;
    const buf = this.outCtx.createBuffer(1, f32.length, OUTPUT_RATE);
    buf.copyToChannel(f32, 0);
    const node = this.outCtx.createBufferSource();
    node.buffer = buf;
    node.connect(this.outCtx.destination); // speakers
    node.connect(this.playDest); // orb analyser tap
    const now = this.outCtx.currentTime;
    const startAt = Math.max(now, this.playCursor);
    node.start(startAt);
    this.playCursor = startAt + buf.duration;
    this.liveSources.add(node);
    node.onended = () => this.liveSources.delete(node);
  }

  private stopAllPlayback() {
    for (const s of this.liveSources) {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    }
    this.liveSources.clear();
    this.playCursor = 0;
  }

  // Gemini usageMetadata: promptTokenCount / responseTokenCount with per-modality
  // breakdown in {prompt,response}TokensDetails[].{modality,tokenCount}.
  private accumulateUsage(u: any) {
    const acc = this.usage;
    const tally = (details: any[], audioKey: "audioInTokens" | "audioOutTokens", textKey: "textInTokens" | "textOutTokens") => {
      for (const d of details || []) {
        const n = d.tokenCount || 0;
        if (String(d.modality).toUpperCase() === "AUDIO") acc[audioKey] += n;
        else acc[textKey] += n;
      }
    };
    if (u.promptTokensDetails || u.responseTokensDetails) {
      tally(u.promptTokensDetails, "audioInTokens", "textInTokens");
      tally(u.responseTokensDetails, "audioOutTokens", "textOutTokens");
    } else {
      // Fallback: treat all prompt as audio-in, all response as audio-out.
      acc.audioInTokens += u.promptTokenCount || 0;
      acc.audioOutTokens += u.responseTokenCount || 0;
    }
    recomputeCost(acc, this.activeModel || this.opts.model);
    this.opts.onEvent({ type: "usage", usage: { ...acc } });
  }
}
