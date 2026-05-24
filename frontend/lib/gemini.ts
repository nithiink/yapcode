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
  COST_SAVER_BREVITY,
  RealtimeEvent,
  RealtimeOptions,
  ToolDef,
  VoiceSession,
  VoiceUsage,
  emptyUsage,
  recomputeCost,
} from "./voice";

const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;

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

// OpenAI-style tool defs -> Gemini functionDeclarations.
function toGeminiTools(tools: ToolDef[]): unknown[] {
  if (!tools.length) return [];
  return [
    {
      functionDeclarations: tools.map((t) => ({
        name: t.name,
        description: t.description,
        parameters: t.parameters,
      })),
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
  private assistantText = "";
  private userText = "";
  private usage: VoiceUsage = emptyUsage();

  constructor(opts: RealtimeOptions) {
    this.opts = opts;
  }

  async start(_audioEl: HTMLAudioElement): Promise<void> {
    const emit = this.opts.onEvent;
    emit({ type: "status", status: "Minting token..." });

    const [sessionRes, toolsRes] = await Promise.all([
      fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "gemini",
          model: this.opts.model,
          voice: this.opts.voice,
        }),
      }),
      fetch("/api/tools"),
    ]);

    if (!sessionRes.ok) throw new Error(`Session error: ${await sessionRes.text()}`);
    const session = await sessionRes.json();
    const token: string = session.value;
    const wsUrl: string = session.ws_url;
    if (!token || !wsUrl) throw new Error("Gemini session response missing token/ws_url");
    this.activeModel = session.model;
    const voice: string = session.voice || this.opts.voice || "Kore";
    if (toolsRes.ok) this.tools = (await toolsRes.json()).tools || [];

    // Prepare audio graph before the socket opens so we can stream immediately.
    await this.initAudio();

    emit({ type: "status", status: "Connecting to Gemini..." });
    const ws = new WebSocket(`${wsUrl}?access_token=${encodeURIComponent(token)}`);
    this.ws = ws;

    ws.addEventListener("open", () => this.sendSetup(session.model, voice));
    ws.addEventListener("message", (ev) => this.onMessage(ev.data));
    ws.addEventListener("error", () =>
      emit({ type: "error", message: "Gemini WebSocket error" }),
    );
    ws.addEventListener("close", (ev) => {
      if (!ev.wasClean) emit({ type: "error", message: `Gemini socket closed (${ev.code})` });
    });
  }

  stop(): void {
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

  // --- audio setup --------------------------------------------------------
  private async initAudio() {
    this.micStream = await navigator.mediaDevices.getUserMedia({ audio: true });

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
    this.playDest = this.outCtx.createMediaStreamDestination();
    // Tap playback into a MediaStream so the orb analyser reacts to the voice.
    this.opts.onRemoteStream?.(this.playDest.stream);
  }

  private onMicChunk(data: { pcm: ArrayBuffer; rms: number }) {
    if (!this.setupDone || this.ws?.readyState !== WebSocket.OPEN) return;
    this.opts.onEvent({ type: "state", state: data.rms > 0.02 ? "hearing" : "listening" });
    this.ws.send(
      JSON.stringify({
        realtimeInput: {
          audio: { data: b64encode(data.pcm), mimeType: `audio/pcm;rate=${INPUT_RATE}` },
        },
      }),
    );
  }

  // --- protocol -----------------------------------------------------------
  private sendSetup(model: string, voice: string) {
    const cost = !!this.opts.costSaver;
    const instructions = this.opts.instructions + (cost ? COST_SAVER_BREVITY : "");
    const setup: Record<string, unknown> = {
      model: model.startsWith("models/") ? model : `models/${model}`,
      generationConfig: {
        responseModalities: ["AUDIO"],
        speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice } } },
        ...(cost ? { maxOutputTokens: 250 } : {}),
      },
      systemInstruction: { parts: [{ text: instructions }] },
      tools: toGeminiTools(this.tools),
      inputAudioTranscription: {},
      outputAudioTranscription: {},
      sessionResumption: {},
    };
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
      emit({ type: "status", status: "Connected — start talking." });
      emit({ type: "state", state: "listening" });
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

    if (msg.usageMetadata) this.accumulateUsage(msg.usageMetadata);
  }

  private async runFunctionCall(fc: { id?: string; name: string; args?: any }) {
    const emit = this.opts.onEvent;
    emit({ type: "tool_call", name: fc.name, arguments: fc.args });

    let result: unknown;
    let ok = true;
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: fc.name, arguments: fc.args || {} }),
      });
      const out = await r.json();
      result = out.result ?? out;
      ok = !!out.ok;
    } catch (e: any) {
      ok = false;
      result = { error: e?.message || String(e) };
    }
    emit({ type: "tool_call", name: fc.name, arguments: fc.args, result, ok });

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
    node.connect(this.playDest);
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
