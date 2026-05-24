// Browser <-> OpenAI Realtime (GA) over WebRTC.
//
// Flow:
//  1. POST /api/session -> backend mints an ephemeral token (data.value, "ek_...").
//  2. Open RTCPeerConnection, attach mic, create a data channel.
//  3. POST the SDP offer to https://api.openai.com/v1/realtime/calls with the ek_ token.
//  4. Audio flows over the peer connection; events over the data channel.
//  5. On a function_call event, POST it to /api/tools/execute, then return a
//     function_call_output item and ask the model to continue.

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

export type RealtimeEvent =
  | { type: "status"; status: string }
  | { type: "state"; state: VoiceState }
  | { type: "transcript"; role: "user" | "assistant"; text: string; final: boolean }
  | { type: "tool_call"; name: string; arguments?: unknown; result?: unknown; ok?: boolean }
  | { type: "error"; message: string };

export type RealtimeOptions = {
  model: string;
  voice: string;
  instructions: string;
  onEvent: (e: RealtimeEvent) => void;
  onRemoteStream?: (stream: MediaStream) => void;
};

const CALLS_URL = "https://api.openai.com/v1/realtime/calls";

export class RealtimeSession {
  private pc?: RTCPeerConnection;
  private dc?: RTCDataChannel;
  private localStream?: MediaStream;
  private audioEl?: HTMLAudioElement;
  private tools: ToolDef[] = [];
  private opts: RealtimeOptions;
  activeModel?: string; // actual model/deployment reported by the backend
  private assistantText = new Map<string, string>();
  private userText = new Map<string, string>();

  constructor(opts: RealtimeOptions) {
    this.opts = opts;
  }

  async start(audioEl: HTMLAudioElement): Promise<void> {
    this.audioEl = audioEl;
    const emit = this.opts.onEvent;
    emit({ type: "status", status: "Minting token..." });

    const [sessionRes, toolsRes] = await Promise.all([
      fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: this.opts.model,
          voice: this.opts.voice,
        }),
      }),
      fetch("/api/tools"),
    ]);

    if (!sessionRes.ok) throw new Error(`Session error: ${await sessionRes.text()}`);
    const session = await sessionRes.json();
    // GA returns the ephemeral token at top-level `value`; tolerate older shapes.
    const ephemeral: string =
      session.value || session.client_secret?.value || session.client_secret;
    if (!ephemeral) throw new Error("No ephemeral token in /session response");
    // Backend tells us where to POST the SDP offer (Azure resource or OpenAI).
    const webrtcUrl: string =
      session.webrtc_url || `${CALLS_URL}?model=${encodeURIComponent(this.opts.model)}`;
    if (session.model) this.activeModel = session.model;

    if (toolsRes.ok) this.tools = (await toolsRes.json()).tools || [];

    emit({ type: "status", status: "Opening WebRTC..." });
    const pc = new RTCPeerConnection();
    this.pc = pc;

    pc.ontrack = (ev) => {
      if (this.audioEl) {
        this.audioEl.srcObject = ev.streams[0];
        this.audioEl.autoplay = true;
        this.audioEl.play().catch(() => undefined);
      }
      this.opts.onRemoteStream?.(ev.streams[0]);
    };

    this.localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    for (const track of this.localStream.getTracks()) pc.addTrack(track, this.localStream);

    const dc = pc.createDataChannel("oai-events");
    this.dc = dc;
    dc.addEventListener("open", () => this.configureSession());
    dc.addEventListener("message", (ev) => this.handleEvent(ev.data));

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    emit({ type: "status", status: "Negotiating WebRTC..." });
    const sdpResp = await fetch(webrtcUrl, {
      method: "POST",
      body: offer.sdp,
      headers: {
        Authorization: `Bearer ${ephemeral}`,
        "Content-Type": "application/sdp",
      },
    });
    if (!sdpResp.ok) throw new Error(`SDP exchange failed: ${sdpResp.status} ${await sdpResp.text()}`);
    await pc.setRemoteDescription({ type: "answer", sdp: await sdpResp.text() });

    emit({ type: "status", status: "Connected — start talking." });
    emit({ type: "state", state: "listening" });
  }

  stop(): void {
    this.dc?.close();
    this.pc?.getSenders().forEach((s) => s.track?.stop());
    this.localStream?.getTracks().forEach((t) => t.stop());
    this.pc?.close();
    this.pc = undefined;
    this.dc = undefined;
    this.localStream = undefined;
    if (this.audioEl) this.audioEl.srcObject = null;
    this.opts.onEvent({ type: "status", status: "Disconnected." });
  }

  private send(payload: Record<string, unknown>) {
    this.dc?.send(JSON.stringify(payload));
  }

  private configureSession() {
    // GA session.update: nested `session.type:"realtime"`, audio config nested.
    this.send({
      type: "session.update",
      session: {
        type: "realtime",
        instructions: this.opts.instructions,
        tools: this.tools,
        tool_choice: "auto",
        audio: {
          // Input transcription intentionally disabled to avoid the separate
          // per-minute transcription charge — the model understands speech
          // directly. (Re-add `transcription: { model: ... }` to show the
          // user's words in the UI.) turn_detection is required for turn-taking.
          input: {
            turn_detection: { type: "server_vad" },
          },
          output: { voice: this.opts.voice },
        },
      },
    });
  }

  private async handleEvent(raw: string) {
    let evt: any;
    try {
      evt = JSON.parse(raw);
    } catch {
      return;
    }
    const emit = this.opts.onEvent;

    switch (evt.type) {
      // --- voice-state signals (drive the orb) ---
      case "input_audio_buffer.speech_started":
        emit({ type: "state", state: "hearing" });
        break;
      case "input_audio_buffer.speech_stopped":
        emit({ type: "state", state: "thinking" });
        break;
      case "response.created":
        emit({ type: "state", state: "thinking" });
        break;
      case "output_audio_buffer.started":
        emit({ type: "state", state: "speaking" });
        break;
      case "response.completed":
      case "output_audio_buffer.stopped":
        emit({ type: "state", state: "listening" });
        break;

      // user speech transcription
      case "conversation.item.input_audio_transcription.delta": {
        const cur = (this.userText.get(evt.item_id) || "") + (evt.delta || "");
        this.userText.set(evt.item_id, cur);
        emit({ type: "transcript", role: "user", text: cur, final: false });
        break;
      }
      case "conversation.item.input_audio_transcription.completed": {
        const text = evt.transcript || this.userText.get(evt.item_id) || "";
        this.userText.delete(evt.item_id);
        emit({ type: "transcript", role: "user", text, final: true });
        break;
      }
      // assistant speech transcription (GA + legacy event names)
      case "response.output_audio_transcript.delta":
      case "response.audio_transcript.delta": {
        const id = evt.response_id || "cur";
        const cur = (this.assistantText.get(id) || "") + (evt.delta || "");
        this.assistantText.set(id, cur);
        emit({ type: "state", state: "speaking" });
        emit({ type: "transcript", role: "assistant", text: cur, final: false });
        break;
      }
      case "response.output_audio_transcript.done":
      case "response.audio_transcript.done": {
        const id = evt.response_id || "cur";
        const text = evt.transcript || this.assistantText.get(id) || "";
        this.assistantText.delete(id);
        emit({ type: "transcript", role: "assistant", text, final: true });
        break;
      }
      // function calls: GA emits them inside response.done output[]
      case "response.done": {
        const out = evt.response?.output || [];
        let hadCall = false;
        for (const item of out) {
          if (item.type === "function_call") {
            hadCall = true;
            await this.runFunctionCall(item);
          }
        }
        // If a tool ran we just asked it to continue; otherwise we're idle again.
        emit({ type: "state", state: hadCall ? "thinking" : "listening" });
        break;
      }
      case "error": {
        emit({ type: "error", message: evt.error?.message || JSON.stringify(evt) });
        break;
      }
      default:
        break;
    }
  }

  private async runFunctionCall(item: any) {
    const name: string = item.name;
    const callId: string = item.call_id;
    let args: any = {};
    try {
      args = item.arguments ? JSON.parse(item.arguments) : {};
    } catch {
      args = {};
    }
    this.opts.onEvent({ type: "tool_call", name, arguments: args });

    let result: unknown;
    let ok = true;
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, arguments: args }),
      });
      const data = await r.json();
      result = data.result ?? data;
      ok = !!data.ok;
    } catch (e: any) {
      ok = false;
      result = { error: e?.message || String(e) };
    }
    this.opts.onEvent({ type: "tool_call", name, arguments: args, result, ok });

    this.send({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: callId, output: JSON.stringify(result) },
    });
    this.send({ type: "response.create" });
  }
}
