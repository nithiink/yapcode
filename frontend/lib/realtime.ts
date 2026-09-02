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
  RealtimeEvent,
  RealtimeOptions,
  ToolDef,
  VoiceSession,
  VoiceUsage,
  emptyUsage,
  recomputeCost,
} from "./voice";
import { authHeaders } from "./auth";
import { enqueueInjection, type PendingInjection } from "./narration";

const CALLS_URL = "https://api.openai.com/v1/realtime/calls";

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
  // A response is in flight from response.created until response.done /
  // response.completed. injectUpdate fires response.create which errors with
  // "conversation_already_has_active_response" while one is active — the
  // narration system message lands in the conversation but is never narrated,
  // so the user thinks the voice agent "missed" it. Queue updates and drain
  // when the current response ends. The queue is bounded (see
  // MAX_PENDING_INJECTIONS): verbose mode can publish faster than one
  // response.create per item drains.
  private responseActive = false;
  private pendingInjections: PendingInjection[] = [];
  // call_ids we've already dispatched to /api/tools/execute. Realtime can
  // surface the same function_call twice — streamed via response.output_item.done
  // AND again in response.done's output[]. Dedupe so we run each call once.
  private dispatchedCalls = new Set<string>();
  // A tool result has been submitted and the model owes us a follow-up response,
  // but a response was still active when we tried to ask for it. Fire the
  // response.create as soon as the active response ends (response.done).
  private awaitingContinuation = false;
  // Backstop timer for a deferred continuation. Azure WebRTC never emits
  // response.done (see the conversation.item.added case), so responseActive
  // sticks true after the first continuation and every later tool result would
  // strand — the model freezes after a tool call (esp. an error result, which
  // makes the model retry immediately, so a second continuation lands while the
  // first is still "active"). If response.done doesn't clear the deferral in
  // time, this fires it anyway. No-op on OpenAI/Gemini, which DO send
  // response.done — it clears awaitingContinuation first, so the timer finds
  // nothing to do.
  private continuationFallback: ReturnType<typeof setTimeout> | null = null;
  private static CONTINUATION_FALLBACK_MS = 1500;
  // Function-call arguments stream as response.function_call_arguments.delta and
  // are NOT included in the conversation.item.added item that Azure uses to
  // surface the call — so accumulate them by call_id and dispatch with the
  // complete string. Maps call_id -> partial/complete arguments JSON, and
  // call_id -> tool name (so a later arguments.done can find the name).
  private callArgs = new Map<string, string>();
  private callNames = new Map<string, string>();
  // Grace timers for calls surfaced by conversation.item.added with EMPTY args
  // (see that case for the per-provider ordering story). Cleared on stop().
  private callFallbackTimers = new Map<string, ReturnType<typeof setTimeout>>();

  constructor(opts: RealtimeOptions) {
    this.opts = opts;
  }

  private trace(msg: string) {
    console.debug("[realtime]", msg);
    this.opts.onDebug?.(msg);
  }

  async start(audioEl: HTMLAudioElement): Promise<void> {
    this.audioEl = audioEl;
    const emit = this.opts.onEvent;
    this.trace(`start provider=${this.opts.provider} model=${this.opts.model}`);
    emit({ type: "status", status: "Minting token..." });

    const [sessionRes, toolsRes] = await Promise.all([
      fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          provider: this.opts.provider,
          model: this.opts.model,
          voice: this.opts.voice,
          // Azure's ephemeral WebRTC session binds its config at mint time and
          // ignores the client session.update — so tools/instructions must be
          // baked into the mint or the model never gets them. Harmless for
          // OpenAI (which also honors the later session.update).
          instructions: this.opts.instructions,
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

    // Explicit DSP constraints: echo of the assistant's own voice re-entering
    // the mic reads as user speech to server VAD, which cancels the response
    // mid-sentence (observed as 4x output_audio_buffer.cleared in one session).
    this.localStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.opts.onLocalStream?.(this.localStream); // feed the user's mic to the orb analyser
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
    for (const t of this.callFallbackTimers.values()) clearTimeout(t);
    this.callFallbackTimers.clear();
    if (this.continuationFallback) {
      clearTimeout(this.continuationFallback);
      this.continuationFallback = null;
    }
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

  injectUpdate(text: string, opts?: { blocking?: boolean }): void {
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
      // A blocking line (permission / question) can never be evicted by the
      // bound — the frontend half of the backend's ALWAYS_SPEAK guarantee. A
      // dropped ask is unrecoverable: poll_status hands back each buffered
      // result once, so it is never re-offered.
      const dropped = enqueueInjection(this.pendingInjections,
                                       { text, blocking: opts?.blocking });
      console.log("[realtime] injectUpdate queued (response in flight):", text.slice(0, 80));
      if (dropped) {
        // Back-pressure, not a bug: the dropped lines are still in the model's
        // conversation, they just don't each get a spoken response.
        console.warn(`[realtime] injection queue full — dropped ${dropped} older texture line(s)`);
      }
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
    const session: Record<string, unknown> = {
      type: "realtime",
      instructions: this.opts.instructions,
      tools: this.tools,
      tool_choice: "auto",
      // Lock output to audio. Left unset, the model sometimes emits a text-only
      // message item within a response (e.g. an audio preamble item followed by
      // a text answer item) — it's transcribed but never spoken, so the user
      // hears only the first item. ["audio"] still yields a transcript.
      output_modalities: ["audio"],
      audio: {
        // Input transcription stays off to avoid the separate per-minute
        // transcription charge — the model understands speech directly.
        // VAD stays at server defaults (tested-good); explicit DSP on
        // getUserMedia handles echo so the assistant doesn't barge itself in.
        input: { turn_detection: { type: "server_vad" } },
        output: { voice: this.opts.voice },
      },
    };

    this.send({ type: "session.update", session });
  }

  private async handleEvent(raw: string) {
    let evt: any;
    try {
      evt = JSON.parse(raw);
    } catch {
      return;
    }
    const emit = this.opts.onEvent;
    // Trace every event type. Audio deltas are far too noisy, but the
    // function_call_arguments deltas are exactly what we need to see, so only
    // filter the audio ones. Include the carried item's type/name.
    if (!/audio.*\.delta$|output_audio_transcript\.delta$/.test(evt.type)) {
      const it = evt.item;
      const extra = it
        ? ` item.type=${it.type}` +
          (it.type === "function_call" ? ` name=${it.name} args=${(it.arguments || "").slice(0, 60)}` : "")
        : "";
      this.trace(`evt ${evt.type}${extra}`);
    }

    switch (evt.type) {
      // Function-call arguments stream as deltas, then a *.done with the full
      // string. Accumulate by call_id so we always dispatch with complete args.
      case "response.function_call_arguments.delta": {
        const id = evt.call_id || evt.item_id;
        if (id) this.callArgs.set(id, (this.callArgs.get(id) || "") + (evt.delta || ""));
        break;
      }
      case "response.function_call_arguments.done": {
        const id = evt.call_id || evt.item_id;
        if (id) {
          const argsStr = evt.arguments ?? this.callArgs.get(id) ?? "";
          this.callArgs.set(id, argsStr);
          const name = this.callNames.get(id) || evt.name;
          if (name) {
            emit({ type: "state", state: "thinking" });
            await this.runFunctionCall(name, id, argsStr);
          }
        }
        break;
      }
      // Providers disagree on how a function call surfaces, so this case is a
      // conditional dispatcher:
      //  - Azure WebRTC omits the response.* lifecycle (no argument deltas, no
      //    arguments.done, no output_item.done) and surfaces the call's complete
      //    arguments in conversation.item.DONE (handled below), NOT here:
      //    conversation.item.added fires at call START with EMPTY arguments.
      //  - OpenAI sends this item FIRST with EMPTY arguments; the deltas stream
      //    after, then arguments.done / output_item.done / response.done carry
      //    the complete string. Dispatching here ran tools with {} (backend
      //    KeyError 'session_id') and the dedup then blocked the real dispatch.
      // So: dispatch now only if we already have args; otherwise register the
      // name and let a completion event (output_item.done / conversation.item.done)
      // dispatch — with a grace-timer fallback so a provider that genuinely sends
      // no arguments at all can't stall the turn.
      case "conversation.item.added":
      case "conversation.item.created": {
        const item = evt.item;
        if (item?.type === "function_call" && item.call_id) {
          this.callNames.set(item.call_id, item.name);
          let argsStr =
            item.arguments && item.arguments.trim()
              ? item.arguments
              : this.callArgs.get(item.call_id) || "";
          // A tool with NO parameters has nothing to stream — empty args ARE
          // complete. Dispatch now instead of burning the grace timer
          // (which is what every zero-param call on Azure would otherwise do).
          if (!argsStr) {
            const def: any = this.tools.find((t: any) => t.name === item.name);
            if (def && Object.keys(def.parameters?.properties || {}).length === 0) {
              argsStr = "{}";
            }
          }
          if (argsStr) {
            this.trace(`conversation.item → function_call ${item.name} args=${argsStr.slice(0, 60)}`);
            emit({ type: "state", state: "thinking" });
            await this.runFunctionCall(item.name, item.call_id, argsStr);
          } else if (!this.dispatchedCalls.has(item.call_id) && !this.callFallbackTimers.has(item.call_id)) {
            this.trace(`conversation.item → function_call ${item.name} awaiting args (fallback armed)`);
            const callId = item.call_id;
            const name = item.name;
            this.callFallbackTimers.set(
              callId,
              setTimeout(() => {
                this.callFallbackTimers.delete(callId);
                if (this.dispatchedCalls.has(callId)) return; // completion event won
                const late = this.callArgs.get(callId) || "";
                // 2.5s gives the complete-args events (output_item.done /
                // conversation.item.done) time to win before this freeze-guard
                // dispatches. If args are STILL empty, the provider sent none at
                // all — flag it loudly (it surfaces a tool error the model can
                // recover from rather than a silent stall).
                if (!late) {
                  this.trace(`fallback dispatch ${name} with NO args — provider delivered none (no output_item.done / conversation.item.done)`);
                } else {
                  this.trace(`fallback dispatch ${name} args=${late.slice(0, 60)}`);
                }
                emit({ type: "state", state: "thinking" });
                void this.runFunctionCall(name, callId, late);
              }, 2500),
            );
          }
        }
        break;
      }
      // OpenAI streams the completed call here (complete args in item.arguments)
      // before response.done — dispatch immediately (deduped by call_id).
      case "response.output_item.done": {
        const item = evt.item;
        if (item?.type === "function_call" && item.call_id) {
          this.trace(`output_item.done → function_call ${item.name}`);
          emit({ type: "state", state: "thinking" });
          await this.runFunctionCall(item.name, item.call_id, item.arguments || "");
        }
        break;
      }
      // Azure's WebRTC path omits the response.* lifecycle (no
      // response.output_item.done, no response.function_call_arguments.done), so
      // per the Realtime spec the COMPLETE function-call arguments arrive here:
      // conversation.item.done finalizes the item with its full `arguments`
      // string + call_id. Without this case the only Azure event we read was
      // conversation.item.added, which fires at call START with EMPTY arguments —
      // so session tools dispatched with no session_id and looped forever on
      // "which session?". Deduped by call_id; cancels the grace-timer fallback.
      case "conversation.item.done": {
        const item = evt.item;
        if (item?.type === "function_call" && item.call_id) {
          this.trace(`conversation.item.done → function_call ${item.name} args=${(item.arguments || "").slice(0, 60)}`);
          emit({ type: "state", state: "thinking" });
          await this.runFunctionCall(item.name, item.call_id, item.arguments || "");
        }
        break;
      }
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
      // GA emits function calls inside response.done output[] too. The response
      // that just finished is no longer active — clear the flag first so any
      // continuation we now request actually fires.
      case "response.done": {
        this.accumulateUsage(evt.response?.usage);
        this.responseActive = false;
        const out = evt.response?.output || [];
        this.trace(
          `response.done status=${evt.response?.status} output=[${out.map((i: any) => i.type).join(",") || "∅"}]`,
        );
        // Dispatch any calls not already run via response.output_item.done.
        // runFunctionCall dedupes on call_id and, since no response is active,
        // requestContinuation will fire the follow-up response.create.
        for (const item of out) {
          if (item.type === "function_call") {
            await this.runFunctionCall(item.name, item.call_id, item.arguments || "");
          }
        }
        // A tool ran via the streaming path and its continuation was deferred
        // until this response ended — fire it now.
        if (this.awaitingContinuation) this.requestContinuation();
        if (this.responseActive) {
          // A continuation (or tool follow-up) is in flight.
          emit({ type: "state", state: "thinking" });
        } else {
          // Nothing pending — drain any queued narration system messages.
          this.drainPendingInjections();
          emit({ type: "state", state: "listening" });
        }
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
        // narration line would queue silently and never narrate (the current
        // prompt then looks "still processing" forever). Clear the flag, then
        // either fire a pending tool continuation or drain queued updates so the
        // conversation keeps flowing. If a response really was still active, the
        // resulting response.create just races and is handled above.
        this.responseActive = false;
        if (this.awaitingContinuation) this.requestContinuation();
        else this.drainPendingInjections();
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

  // Ask the model to produce its follow-up response after a tool result. Only
  // valid when no response is active — otherwise the API rejects it with
  // 'conversation_already_has_active_response'. If one is active, defer until
  // response.done clears responseActive.
  private requestContinuation() {
    if (this.responseActive) {
      this.awaitingContinuation = true;
      this.trace("continuation deferred (response still active)");
      // Backstop: Azure never sends response.done to clear responseActive, so
      // without this the deferral is permanent and the model freezes after the
      // tool result. response.done (OpenAI/Gemini) fires the continuation and
      // clears awaitingContinuation before this runs, making it a no-op there.
      if (this.continuationFallback) clearTimeout(this.continuationFallback);
      this.continuationFallback = setTimeout(() => {
        this.continuationFallback = null;
        if (!this.awaitingContinuation) return; // response.done already handled it
        this.trace("continuation fallback fired (no response.done — Azure path)");
        this.responseActive = false;
        this.requestContinuation();
      }, RealtimeSession.CONTINUATION_FALLBACK_MS);
      return;
    }
    if (this.continuationFallback) {
      clearTimeout(this.continuationFallback);
      this.continuationFallback = null;
    }
    this.awaitingContinuation = false;
    this.responseActive = true;
    this.trace("continuation → response.create");
    this.send({ type: "response.create" });
  }

  private async runFunctionCall(name: string, callId: string, argsStr: string) {
    // Each function_call can surface on several paths (conversation.item.added,
    // arguments.done, output_item.done, response.done) — run it exactly once.
    if (callId && this.dispatchedCalls.has(callId)) return;
    if (callId) {
      this.dispatchedCalls.add(callId);
      const t = this.callFallbackTimers.get(callId);
      if (t) {
        clearTimeout(t);
        this.callFallbackTimers.delete(callId);
      }
    }
    this.trace(`dispatch tool ${name} (call_id=${callId}) args=${(argsStr || "").slice(0, 60)}`);
    let args: any = {};
    try {
      args = argsStr && argsStr.trim() ? JSON.parse(argsStr) : {};
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
    this.trace(`tool ${name} result ok=${ok}; sending function_call_output`);
    this.opts.onEvent({ type: "tool_call", name, arguments: args, result, ok });

    this.send({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: callId, output: JSON.stringify(result) },
    });
    // The model needs to respond to the tool result. Fire now if idle, else
    // defer until the active response finishes (avoids a racy reject that would
    // strand the result and leave the orb stuck "thinking").
    this.requestContinuation();
  }
}
