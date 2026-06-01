// Browser <-> OpenAI / Azure Realtime (GA) over WebRTC.
//
// Flow:
//  1. POST /api/session -> backend mints an ephemeral token (data.value, "ek_...").
//  2. Open RTCPeerConnection, attach mic, create a data channel.
//  3. POST the SDP offer to the backend-provided webrtc_url with the ek_ token.
//  4. Audio flows over the peer connection; events over the data channel.
//  5. On a function_call event, POST it to /api/tools/execute, then return a
//     function_call_output item and ask the model to continue.

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
import { authHeaders } from "./auth";

const CALLS_URL = "https://api.openai.com/v1/realtime/calls";

// Cost-saver knobs. The dominant realtime cost is audio tokens, and the whole
// conversation is re-sent every turn — so we cap response length, tighten VAD
// to avoid spurious responses, and prune old turns to bound the re-billing.
const COST_SAVER_MAX_OUTPUT_TOKENS = 200;
// Was 6 — that's ~3 turns and dropped both prior user requests and the
// [Claude update] system messages, making the model "forget" what was just
// discussed and babble about stale Claude output. 30 keeps a useful window
// without unbounded growth.
const COST_SAVER_KEEP_ITEMS = 30;

export class RealtimeSession implements VoiceSession {
  private pc?: RTCPeerConnection;
  private dc?: RTCDataChannel;
  private localStream?: MediaStream;
  private audioEl?: HTMLAudioElement;
  private tools: ToolDef[] = [];
  private opts: RealtimeOptions;
  activeModel?: string;
  private assistantText = new Map<string, string>();
  private userText = new Map<string, string>();
  private usage: VoiceUsage = emptyUsage();
  // Ordered conversation item ids, for cost-saver context pruning.
  private itemIds: string[] = [];
  // A response is in flight from response.created until response.done /
  // response.completed. injectUpdate fires response.create which errors with
  // "conversation_already_has_active_response" while one is active — the
  // [Claude update] system message lands in the conversation but is never
  // narrated, so the user thinks the voice agent "missed" it. Queue updates
  // and drain when the current response ends.
  private responseActive = false;
  private pendingInjections: string[] = [];

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
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          provider: this.opts.provider,
          model: this.opts.model,
          voice: this.opts.voice,
        }),
      }),
      fetch("/api/tools", { headers: authHeaders() }),
    ]);

    if (!sessionRes.ok) throw new Error(`Session error: ${await sessionRes.text()}`);
    const session = await sessionRes.json();
    // GA returns the ephemeral token at top-level `value`; tolerate older shapes.
    const ephemeral: string =
      session.value || session.client_secret?.value || session.client_secret;
    if (!ephemeral) throw new Error("No ephemeral token in /session response");
    const webrtcUrl: string =
      session.webrtc_url || `${CALLS_URL}?model=${encodeURIComponent(this.opts.model || "")}`;
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

  setMuted(muted: boolean): void {
    this.localStream?.getAudioTracks().forEach((t) => (t.enabled = !muted));
  }

  injectUpdate(text: string): void {
    if (!this.dc || this.dc.readyState !== "open") {
      console.warn("[realtime] injectUpdate dropped — data channel not open:", text.slice(0, 80));
      return;
    }
    // Always add the system message to the conversation immediately so the
    // model sees it whenever it next responds. But only call response.create if
    // no response is in flight — otherwise queue it and fire on response.done.
    this.send({
      type: "conversation.item.create",
      item: { type: "message", role: "system", content: [{ type: "input_text", text }] },
    });
    if (this.responseActive) {
      this.pendingInjections.push(text);
      console.log("[realtime] injectUpdate queued (response in flight):", text.slice(0, 80));
    } else {
      this.responseActive = true;
      this.send({ type: "response.create" });
    }
  }

  private drainPendingInjections(): void {
    if (this.pendingInjections.length === 0 || !this.dc || this.dc.readyState !== "open") return;
    // Narrate exactly ONE queued update per response. Each queued item is
    // already in the conversation (its own conversation.item.create), so we just
    // need one response.create per item. response.done with hadCall=false calls
    // this again, draining the rest in FIFO order — so N updates yield N
    // narrations. (The old code zeroed the whole queue and fired a single
    // response.create, which collapsed N updates into one narration: the model
    // read the oldest and called the newest "still processing".)
    this.pendingInjections.shift();
    this.responseActive = true;
    this.send({ type: "response.create" });
  }

  private configureSession() {
    const cost = !!this.opts.costSaver;
    // Tighter turn detection in cost mode: longer required silence + higher
    // threshold => fewer spurious responses (each response costs audio tokens).
    const turnDetection = cost
      ? { type: "server_vad", threshold: 0.6, silence_duration_ms: 700 }
      : { type: "server_vad" };

    const instructions = this.opts.instructions + (cost ? COST_SAVER_BREVITY : "");

    const session: Record<string, unknown> = {
      type: "realtime",
      instructions,
      tools: this.tools,
      tool_choice: "auto",
      audio: {
        // Input transcription stays off to avoid the separate per-minute
        // transcription charge — the model understands speech directly.
        input: { turn_detection: turnDetection },
        output: { voice: this.opts.voice },
      },
    };
    if (cost) session.max_output_tokens = COST_SAVER_MAX_OUTPUT_TOKENS;

    this.send({ type: "session.update", session });
  }

  // Cost-saver: keep only the most recent items so the per-turn re-sent audio
  // context (and its token bill) stays bounded instead of growing every turn.
  private pruneContext() {
    if (!this.opts.costSaver) return;
    while (this.itemIds.length > COST_SAVER_KEEP_ITEMS) {
      const id = this.itemIds.shift();
      if (id) this.send({ type: "conversation.item.delete", item_id: id });
    }
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
      case "conversation.item.created":
        if (evt.item?.id) this.itemIds.push(evt.item.id);
        break;

      // --- voice-state signals (drive the orb) ---
      case "input_audio_buffer.speech_started":
        emit({ type: "state", state: "hearing" });
        break;
      case "input_audio_buffer.speech_stopped":
        emit({ type: "state", state: "thinking" });
        break;
      case "response.created":
        this.responseActive = true;
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
        this.accumulateUsage(evt.response?.usage);
        const out = evt.response?.output || [];
        let hadCall = false;
        for (const item of out) {
          if (item.type === "function_call") {
            hadCall = true;
            await this.runFunctionCall(item);
          }
        }
        this.pruneContext();
        // The response that just finished is no longer active. runFunctionCall
        // may have started a new one (sets responseActive=true via its own
        // response.create); if not, drain any [Claude update] system messages
        // that were queued while this response was speaking.
        if (!hadCall) {
          this.responseActive = false;
          this.drainPendingInjections();
        }
        emit({ type: "state", state: hadCall ? "thinking" : "listening" });
        break;
      }
      // The server reports remaining tokens/requests after each response. Surface
      // it so we can SEE throttling (esp. the 60 RPM ceiling) instead of guessing.
      case "rate_limits.updated": {
        const limits: any[] = evt.rate_limits || [];
        console.log(
          "[realtime] rate_limits: " +
            limits
              .map((l) => `${l.name} ${l.remaining}/${l.limit} (reset ${l.reset_seconds}s)`)
              .join(" · "),
        );
        const low = limits.find(
          (l) => typeof l.remaining === "number" && l.remaining <= Math.max(2, (l.limit || 0) * 0.1),
        );
        if (low) {
          emit({
            type: "status",
            status: `⚠ ${low.name} rate limit low: ${low.remaining}/${low.limit} left, resets in ${Math.ceil(low.reset_seconds || 0)}s`,
          });
        }
        break;
      }
      case "error": {
        const e = evt.error || {};
        // 'conversation_already_has_active_response' means our response.create
        // raced the model — the system message is still in context, just not
        // narrated. Don't surface it as a user-visible error; just retry when
        // the current response ends (drainPendingInjections handles this on
        // response.done). For other errors, surface normally.
        const racy = e.code === "conversation_already_has_active_response";
        if (racy) {
          console.warn("[realtime] response.create raced an in-flight response; will retry on response.done");
          break;
        }
        // Any non-racy error means the response.create we fired won't produce a
        // response.done — so responseActive would stick true and every later
        // [Claude update] would queue silently and never narrate (the current
        // prompt then looks "still processing" forever). Clear the flag and try
        // to drain so updates keep flowing. If a response really was still
        // active, the drain's response.create just races and is handled above.
        this.responseActive = false;
        this.drainPendingInjections();
        const rate = e.code === "rate_limit_exceeded" || /rate limit/i.test(e.message || "");
        emit({
          type: "error",
          message: (rate ? "Rate limited by the provider: " : "") + (e.message || JSON.stringify(evt)),
        });
        break;
      }
      default:
        break;
    }
  }

  private accumulateUsage(u: any) {
    if (!u) return;
    const ind = u.input_token_details || {};
    const outd = u.output_token_details || {};
    const cd = ind.cached_tokens_details || {};
    const acc = this.usage;
    acc.audioInTokens += ind.audio_tokens || 0;
    acc.textInTokens += ind.text_tokens || 0;
    acc.audioCachedTokens += cd.audio_tokens ?? ind.cached_tokens ?? 0;
    acc.textCachedTokens += cd.text_tokens || 0;
    acc.audioOutTokens += outd.audio_tokens || 0;
    acc.textOutTokens += outd.text_tokens || 0;

    recomputeCost(acc, this.activeModel || this.opts.model);
    this.opts.onEvent({ type: "usage", usage: { ...acc } });
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
    // The app, not the model, chooses the execution backend.
    if (name === "start_session" && this.opts.backend) args.backend = this.opts.backend;
    this.opts.onEvent({ type: "tool_call", name, arguments: args });

    let result: unknown;
    let ok = true;
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
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
    // The model needs to respond to the tool result; this is a fresh response.
    this.responseActive = true;
    this.send({ type: "response.create" });
  }
}
