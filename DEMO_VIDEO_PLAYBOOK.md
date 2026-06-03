# voice-claude — Launch Video Playbook

## 1. TL;DR

Lead with **"THERE IS NO KEYBOARD"** (the paradigm film, top-scored at 84) as the hero, but recut so the first muted second is a locked-wide shot of two motionless human hands beside a working terminal — not a black screen — and the manifesto migrates entirely into the caption. It is the only concept that bundles a falsifiable claim ("the hands never move," shown in one unbroken read) with an argument-shaped hook ("1968 — the mouse / 2007 — the keyboard / 2026 — ___") that senior engineers will literally finish for you in the replies. **One-line reason:** it stops the scroll on disbelief, survives the skeptics because it shows itself denying an `rm`, and gives the X/HN crowd a debate to live inside — which is the only engine that makes a dev tool go viral.

---

## 2. The launch sequence

Treat this as a 14-day campaign with three jobs — **stop the scroll**, **convert to stars**, **sustain the conversation** — not a single drop. The hero film is the spear; the OSS teardown is the conversion machine; the rest are oxygen.

**T-3 to T-1 (pre-seed, build the powder):**
- Pin a single still — the empty-desk frame ("it's still working.") — with a one-line teaser: *"Friday. The keyboard becomes optional."* No link yet. This primes the timeline and tells the algorithm your account is about to matter.
- Cut and QA all assets. Record the **raw, unedited end-to-end session** now — it's your single most important defensive asset and it must exist before launch hour.

**Day 0 — LAUNCH (Tuesday or Wednesday, ~9am ET / 6am PT, before HN's morning):**
- **Post 1 (the spear):** HERO film "THERE IS NO KEYBOARD" as native X video, anchored by the 9-tweet thread. **First reply is the raw uncut run.** This is non-negotiable — it discharges the "faked" accusation before it spreads.
- **Simultaneously:** "Show HN: voice-claude — I drive real Claude Code by voice (open source)" linking the YouTube canonical cut + repo. HN is where stars are won; the film is the bait, the README is the close.
- **Within the hour:** post the 6-second vertical cut-down to Reels / Shorts / TikTok (hero's loop edit). Different audience, different algorithm, same hook.

**Day 1–2 — CONVERT (ride the wave, aim it at GitHub):**
- **Post 2 (the conversion machine):** "GIVING IT AWAY" — the OSS architecture teardown. This is the one whose only job is turning curious viewers into stargazers. Drop the two real code-comment screenshots (`send-keys Backspace` types the word "Backspace"; the paste-detection Enter trick) as standalone PNGs in the thread — they spread even if the video never plays. **Use verified numbers — ~7,800 lines of source (≈11,900 across all tracked files), 111 commits, MIT. Recompute LOC fresh right before posting: `git ls-files | xargs wc -l`.** This is where "respect" converts to a star.
- Cross-post to r/programming with a plain, un-hyped title.

**Day 4–6 — SUSTAIN (broaden past the flex crowd):**
- **Post 3 (the human angle):** "BED MODE (PR EDITION)" — the 6-second loop of triaging a prod bug from bed (fixed to *open a PR on a branch*, never push to main). Relatable, send-to-a-coworker, racks autoplay watch-time. Keeps the campaign alive after the launch-day spike.

**Day 8–12 — DEPTH (reward the converted, restart the debate):**
- **Post 4 (the skeptic):** "THE DARE" — the self-referential bug fix (voice-claude fixing its own message-submit bug, the real `2d10e05`). This is for the people who said "bet it can't" on Day 0. Quote-tweet the original doubters. Lands hardest *after* the tool already has stars, because now skepticism is the underdog position.
- **Optional Post 5:** "THE MAESTRO" (four parallel sessions) as a pure flex clip for the algorithm if momentum is strong.

**Rule across all of it:** every post links the same repo, every post has the raw receipt one click away, and you reply to the smart skeptics personally — their conversion is your second wave.

---

## 3. THE HERO CONCEPT

### Codename: **"THERE IS NO KEYBOARD"** (on-screen end title: *NO STEP 3*)

**Platform + length:** Primary — X/Twitter native video, **1:48**, anchored by a 9-tweet thread. Canonical cut on YouTube (embedded at the top of the README). A **6-second vertical loop** for Reels/Shorts/TikTok using the cold open + the empty-desk payoff. HN "Show HN" links YouTube + repo.

### The exact 3-second hook (recut for the muted feed)

No black screen. No silence-into-void. **Hard cut, frame one, into a locked-wide shot of a real desk in daylight: two human hands resting flat and dead still beside a laptop, the warm orb already mid-breath, and a picture-in-picture of the live Claude TUI already scrolling in the top-right corner.** Motion in the first pixel. Burned-in, small, mono, top-left: `2026 — ____` with a blinking terracotta cursor.

The founder's voice is *already mid-sentence* as the frame appears — no lead-in: **"...watch what I don't touch."** Then immediately: **"Open the API repo, fix the failing auth test."** By 0:02, a real file edit lands in the PiP and `pytest` starts. The hands have not moved.

Why it's unscrollable: living hands + a scrolling terminal + a breathing orb is legible at thumbnail size with sound off; the still hands beside a *working* terminal poses the instant question "wait — who's driving this?"; and `2026 — ____` is caption-bait read in half a second. The poetic "1968 / 2007 / 2026" type-on crawl is **not deleted** — it becomes the slow YouTube cold open and the thumbnail overlay, where viewers have opted in.

### Logline

A manifesto film that *proves* rather than promises that voice is the next input layer for engineering — a founder fixes a real failing test, commands three named sessions, and denies a destructive `rm`, never once touching the keyboard.

### Beat-by-beat shot list

| t | visual | audio | on-screen |
|---|---|---|---|
| 0:00–0:03 | Locked wide: motionless hands beside laptop, orb mid-breath, live TUI in PiP top-right, already scrolling. | (already mid-sentence) "...watch what I don't touch. Open the API repo, fix the failing auth test." Faint room tone. | `2026 — ____` (blinking terracotta cursor) |
| 0:03–0:14 | Slow push on the screen; orb scales with voice. "Listening" in topbar. Not one finger moves toward the keys. | Voice agent (warm, synthetic): "On it — starting a session." | — |
| 0:14–0:26 | Full-bleed live Claude TUI: real file reads, an edit to the test, the runner executing. A pytest line resolves to **PASSED** in green. | Soft real test-run hum. Voice agent: "Found it — expired token fixture. Patched it. Auth suite's green." | `auth_test.py · PASSED` |
| 0:26–0:35 | Cut to the hands — still flat the entire time. A small, involuntary half-laugh. First glimpse of his face, half-lit. | Founder, quiet, to himself: "...I didn't type anything." | — |
| 0:35–0:50 | Session list shows three named live sessions — `jarvis`, `billing-fix`, `docs` — each with its own ticking cost meter. | Founder: "Have billing-fix run the suite. Jarvis, draft the changelog." Voice agent: "Both moving." | `jarvis · billing-fix · docs` |
| 0:50–1:05 | Amber permission card slides in. Hold on it. He shakes his head once. | Founder, flat and certain: "No. Deny that." Voice agent: "Denied — left it untouched." | `Claude wants to run  rm migrations/0012_drop.py  — approve?` |
| 1:05–1:16 | Tight insert: his hand rests ON the keyboard, types one real word into the *same* live session, lifts away as he keeps talking. Co-driving, one transcript. | Founder: "You type when you want to. You talk when you don't." | `voice + keyboard · one session` |
| 1:16–1:30 | He stands, walks out of frame. Hold on the empty desk: laptop alone, orb still breathing, TUI still scrolling. Camera drifts to a half-closed door, warm light under it. | Footsteps. Room goes quiet. A single soft "done" chime from another room. | — |
| 1:30–1:40 | iPhone, handheld, couch: same app, same sessions, on his phone over the LAN. One glance, one line, sets it face-down on his chest. | Founder, relaxed: "Ship it." Voice agent: "Committed and pushed." | `on your phone · over your network` |
| 1:40–1:48 | Hero lockup on charcoal: tall uppercase condensed **SPEAK.** / **CLAUDE BUILDS.** (CLAUDE in terracotta). Below: small. Then black. | Silence. No music sting. | `SPEAK. CLAUDE BUILDS.` / `MIT · drives the real Claude Code CLI` / `github.com/nithiink/voice-claude` |

### Verbatim script

> *(already mid-sentence, hands flat, TUI live in PiP)*
> "...watch what I don't touch. Open the API repo, fix the failing auth test."
> VOICE AGENT: "On it — starting a session."
> VOICE AGENT: "Found it. Expired token fixture — patched it. Auth suite's green."
>
> *(quiet, half-laugh, to himself)*
> "...I didn't type anything."
>
> "Have billing-fix run the suite. Jarvis, draft the changelog."
> VOICE AGENT: "Both moving."
>
> *(amber permission card slides in: `rm migrations/0012_drop.py`)*
> "No. Deny that."
> VOICE AGENT: "Denied. Left it untouched."
>
> *(hand rests on the keyboard, types one word into the same live session, lifts away)*
> "You type when you want to. You talk when you don't."
>
> *(stands, walks out — empty desk, TUI still scrolling — later, couch, phone on chest)*
> "Ship it."
> VOICE AGENT: "Committed and pushed."
>
> *(title)* SPEAK. CLAUDE BUILDS.

The to-camera manifesto speech is **cut**. The mouse/keyboard thesis lives entirely in the caption, where it can't read as arrogant. Exactly one spoken thesis line survives — welded to the co-driving action, so it's narration *over* a real gesture, not a sermon.

### Music / sound direction

Almost no music — that's the taste signal. The cold open and the proof run on **room tone and the real captured sounds of the machine**: the soft hum of a test running, the green-pass tick, the permission card's subtle chime. A single sustained warm sub-bass drone (one low note, barely there — think a felted, held cello) fades in *only* under the empty-desk walk at 1:16, and is the only "music" in the piece. **It does not swell at the end.** Critical silence beats: the full second of quiet on the empty desk before the phone chime. The phone "done" chime from the other room is the emotional period at the end of the sentence. End on silence, not a sting.

### The Jobs-level detail

**The hands.** A locked-off shot of two human hands resting flat and completely motionless on the desk while real code gets written on screen — held long enough to become *uncomfortable*, then resolved with the involuntary half-laugh "I didn't type anything." No graphic explains it; the stillness IS the argument. Second-order: the film refuses a music swell and a logo animation and simply *stops* — restraint as confidence.

### Launch-day caption

> I just fixed a failing test, ran 3 Claude Code sessions at once, and denied an `rm` — without touching my keyboard.
>
> Real Claude Code. My machine. Driven by voice.
>
> 1968 gave us the mouse. 2007 put the keyboard on glass. My bet for 2026: voice — for *writing software*, not just texting.
>
> Open source, MIT. Raw unedited run in the thread so you can call BS. 🧵👇

### README hero GIF frame

The **empty desk at ~1:22**: the laptop alone in warm low light, the terracotta orb caught mid-breath at its fullest scale, the live Claude TUI mid-scroll, the cost meter ticking `$0.01 → $0.02`, and an unoccupied chair. A **2-second seamless loop** (orb breathes in and out one full cycle; the breath rhythm hides the loop seam). Persistent bottom-left caption: **`it's still working.`** It tells the whole story with no face and no text-to-read — the machine works, nobody's there. This is also the thumbnail and the pinned-tweet media; the timeline overlay (`1968 / 2007 / 2026 — ___`) lives here on the static thumbnail version.

### Exactly how the solo founder shoots it

**Two real takes, no actors.**
- **TAKE A (the desk):** iPhone on a cheap tripod, locked wide, daylight from a window camera-left for the warm look. Record continuous audio on a lav or the phone so the "no hands" read is provably one take. Separately screen-record the browser at full res — actually do the tasks in a real repo with a genuinely failing auth test you write beforehand. **Composite the clean screen capture as the PiP in the corner of the framed desk shot — never hard-cut from "hands still" to full-screen.** The cut is exactly where a skeptic stops believing; the PiP keeps hands + terminal + voice in one continuous frame.
- Pre-stage the three named sessions and the `rm` permission prompt by running the real flow once to confirm wording, then perform it.
- **TAKE B (the couch + phone):** handheld iPhone, run the app on the phone over LAN with the auth token, capture the "Ship it / pushed" exchange for real.
- Title cards and hero lockup: screenshot the app's real hero, plus simple text-on-`#1a1917` cards in iMovie/Final Cut. Audio is the founder's real voice; the agent replies are the product's actual TTS, captured from the session.
- **The whole film is room tone + one drone + real machine sounds.** Link the raw unedited run as the literal first reply.

---

## 4. FOUR+ DISTINCT ALTERNATIVE CONCEPTS

Four fundamentally different bets — a generosity flex, a relatable micro-moment, a skeptic reversal, and a reverent product film — plus a power-flex bonus. Different emotion, different audience, different mechanic.

### ALT 1 — "GIVING IT AWAY" (the OSS conversion machine)

- **Angle:** Builder-to-builder respect. A calm, code-forward teardown of the *actual* repo — voice tool call → tmux send-keys → real Claude TUI → phone — proving every claim with real source, then dropping the MIT license: "~7,800 lines of source. One person. I'm giving it all away. Repo's in the replies."
- **3-second hook:** Frame one, the warm app already live — coral orb mid-pulse, real green `claude` TUI streaming. Hard-cut cream caption: **"THIS IS THE REAL CLAUDE CODE CLI. AND I'M TALKING TO IT."** then **"NOT A CLONE. THE ACTUAL `claude`."** Cost meter reads `$0.00 — your login, no API key`.
- **Why it spreads:** Respect is the rarest currency on dev-X. Jaded engineers screenshot the two real code comments (`send-keys Backspace` types the *word* "Backspace"; the paste-detection Enter trick) and quote-tweet "most honest demo I've seen all year." Showing real code signals nothing to hide → skepticism converts to stars. This is the post that actually moves the star counter.
- **Length / platform:** 1:50 on YouTube (chaptered, canonical) + Show HN + r/programming; a 0:45 vertical for the launch tweet; standalone code-comment PNGs in the thread.
- **Shooting notes:** All screen recordings of the real running app + editor scrolling to `backend/tools.py`, `backend/tmux_runner.py`, `backend/tmux_hooks/hook_pretool.py`, `frontend/components/LiveTerminal.tsx`, `LICENSE`. Coral text labels added in any editor. Two physical shots only (laptop closing, phone on couch). **Use verified numbers — ~7,800 lines of source (≈11,900 incl. all tracked files), 111 commits; a `wc -l` will expose a lie, so recompute right before posting (`git ls-files | xargs wc -l`).** Silent until 1:25; one warm synth note enters at the lid close.

### ALT 2 — "BED MODE (PR EDITION)" (the relatable 6-second flex)

- **Angle:** The midnight prod-page nightmare, defused by total physical stillness. A dev triages a real bug from bed without sitting up — and Claude opens a PR on a branch, never pushing to main.
- **3-second hook:** Pitch-dark bedroom, coral orb glow breathing on a jaw and pillow (motion in frame one), phone screen-down on the chest. A single real notification buzz — *the midnight page* — the glow flares. Tired, unperformed mumble: **"claude... the prod error from this morning. fix it on a branch and open a PR."** Burned-in caption of the line; tiny `11:58 PM`.
- **Why it spreads:** Every engineer has been dragged to a laptop at midnight. Stillness vs. an implied production fix triggers "wait, is that real?" — the exact emotion that fuels quote-tweet arguments, and the skeptics do the marketing. It loops in 6 seconds (autoplay watch-time) and the *PR + tests + voice approval* are the receipts that convert "fake" into "oh god it's open source."
- **Length / platform:** 6-second vertical loop as the primary (Shorts/Reels/TikTok/X); a ~13s X cut where the diff, the permission card, and the cost meter each get a legible beat.
- **Shooting notes:** Bedroom plate — lights off, prop the iPhone, run the real app on a second device just off-frame so the genuine orb glow lights you (no fake light). Record audio live so the tired voice is real. Screen capture a real session against a repo where `checkout.ts` genuinely lacks a null check. **Critical: open a PR on a branch — never push to main. The hero beat is the approval ("yeah"), not a deploy.** One sub-bass hit when the PR opens; otherwise silence.

### ALT 3 — "THE DARE" (skeptic-to-believer, self-referential)

- **Angle:** A jaded engineer narrates their own contempt in real time, then eats it — by daring the tool to fix a bug *in its own repo*: the voice driver couldn't even submit a message right.
- **3-second hook:** Full-bleed charcoal, the warm orb large and centered, breathing once. VO already mid-sentence, flat and bored: **"another voice-AI toy. sure."** Camera whip-racks focus past the orb to a real terminal — a dictated message sitting *stuck, unsent, multi-lined* in the input box. VO: "fix the thing in my own repo I've been dodging for a month — you can't even submit a message right." Caption beat: **"(laptop's in the other room.)"**
- **Why it spreads:** The opening line is the exact comment the HN/X crowd was already typing — they see themselves as the protagonist, lower their guard, get reversed. "The voice tool fixed its own voice" is intensely quotable, and the proof is *behavioral and unfakeable* (the stuck message now submits). Endorsing it costs a cynic nothing — they were skeptical too.
- **Length / platform:** 75s on X (captions on by default) + a 4:30 uncut director's cut linked for HN; a 6s loopable vertical.
- **Shooting notes:** Use the **real historical bug, commit `2d10e05`** (paste-detection ate the Enter). **Do NOT fake a "12 passed" pytest run — the repo has no test suite, and that's the most cloneable lie possible.** Proof is the same dictated message going from stuck → submitting on the live terminal. **Do not show a fabricated cost meter** (`claudeTotalUsd` logs as $0.00). Name the commit hash in the caption — it turns skeptics into your fact-checkers. "...huh" sits in total silence; if the acting wobbles, play it as VO over the terminal and cut the face.

### ALT 4 — "THE QUIET MACHINE" (the reverent keynote film)

- **Angle:** Pure Apple-keynote reverence. The flex is *absence* — the laptop is closed in another room while a real session ships code, and the most powerful thing on screen is a person doing nothing.
- **3-second hook:** One continuous handheld shot, evening light. A phone face-up on a couch cushion, the orb breathing; behind it, soft-focus through a doorway, a closed laptop lid glowing at the hinge. Hard text overlay, cream condensed caps, frame one (legible muted): **"MY LAPTOP IS IN THE OTHER ROOM. CLOSED. IT'S WRITING CODE RIGHT NOW."** At 0:03 the orb pulses and the first spoken sentence begins.
- **Why it spreads:** Reverence + disbelief. The closed-laptop fact is front-loaded as the quote-tweet line ("wait, the laptop is CLOSED"). The crowd that dunks on cringe AI demos shares this *because it refuses to be cringe* — "this is how you launch a tool."
- **Length / platform:** 52s on X (pinned) + YouTube canonical + a 6s silent loop for the README. Raw uncut screen-recording pinned as reply #1.
- **Shooting notes:** Four spoken lines total, one felt-piano note, no whooshes/clicks/typing sounds — the absence of keyboard clatter is the point. Screen-record the real app for the orb/terminal/permission card. **Do NOT film the cost meter — it renders `Claude $0.0000` (subscription = zero API cost) with Voice ~$3, a guaranteed dunk.** If you must show cost, recut the copy to the honest, *stronger* line: *"Claude Code: $0.00 — runs on my subscription, no API key."*

### BONUS — "THE MAESTRO" (the power flex, optional sustain)

- **Angle:** One voice, four Claudes. A 2×2 grid of four real terminals; the solo dev becomes a whole engineering team.
- **3-second hook:** Hard cut to four real Claude terminals filling a black screen, all frozen, cursors blinking. Close, dry voice: **"Okay. All four of you — listen."** On "you," all four cursors move and the four orbs flare to voice amplitude.
- **Why it spreads:** Incredulous respect at verifiable scale — four real diffs, a real voice-approved permission prompt. "Four of me. None of them tired." is the quotable. Best as a Day 8+ flex once the tool already has credibility.
- **Length / platform:** 40s on X; pure muted-legible spectacle.
- **Shooting notes:** One screen recording of four real parallel named sessions. Show one genuine git diff and one real voice approval; the honest sub-dollar voice cost and the README sandbox note disarm the "fake" reflex.

---

## 5. The README treatment

**Tagline (above the GIF, set in the cream condensed display face):**
> # SPEAK. CLAUDE BUILDS.
> *A voice front-end for the real Claude Code CLI. You talk; it drives an actual `claude` session on your machine — and streams the live terminal to your phone. Open source, MIT.*

**The GIF that leads the repo:** the **hero film's empty-desk loop** — a 2-second seamless cycle of the laptop alone in warm low light, the terracotta orb breathing one full cycle, the live Claude TUI mid-scroll, the cost meter ticking `$0.01 → $0.02`, persistent caption **`it's still working.`** No face, no text to read: a working machine with nobody there. It is calm, premium, and instantly communicates the whole product.

**Directly beneath it, two links, plainly labeled:** *"▶ Watch the 1:48 film (YouTube)"* and *"Watch the raw, unedited end-to-end session — no music, no cuts."* The GIF earns the look; the raw run earns the trust.

---

## 6. DO / DON'T for shooting

**DO**
- **Use a real codebase with a real, genuinely annoying bug** you actually have. The value should live in the *diagnosis*, not the diff.
- **Keep proof beats as unbroken continuous reads** — hands + screen + voice in one frame (use picture-in-picture, never a hard cut at the moment of magic).
- **Show the product saying NO** — denying an `rm`, asking before a destructive action. A voice tool that refuses is the opposite of the reckless AI hype this crowd dunks on.
- **Lean into real voice latency as a feature** — cut to the orb breathing during waits; "On it — I'll let you know."
- **Use the product's actual TTS and actual TUI output** — the slight cadence and latency are proof it's real.
- **Pin the raw, uncut session as the first reply** and surface the safety model (directory sandbox, auth token, fail-closed) in the thread, in plain text.
- **Open a PR on a branch, not a push to main.** Responsible reads as credible; reckless reads as a dunk.
- **Verify every number against the repo** — ~7,800 lines of source (≈11,900 all-tracked), 111 commits. Recompute LOC at post time (`git ls-files | xargs wc -l`); a clonable lie is fatal.

**DON'T**
- **Don't fake the output.** No fabricated "12 passed" (there's no test suite), no invented `$0.04` cost meter (`claudeTotalUsd` is $0.00 — subscription, no API key). One faked frame torches the whole launch the instant someone clones.
- **Don't add a music swell, a beat-drop, a logo animation, or a hype VO.** Celebration reads as an ad. Restraint reads as confidence.
- **Don't open on a black screen or silence in a muted feed** — motion in frame one, captions carry the hook with sound off.
- **Don't over-act the turn.** The involuntary half-laugh and the flat "...huh" *in silence* are the entire performance. If acting wobbles, go VO-over-screen and cut the face.
- **Don't lecture.** No to-camera manifesto. Put the big idea in the caption, the proof on screen.
- **Don't show a hero shot of code on a giant monitor as the payoff.** The flex is the *absence* of the desk, not a beauty shot of a UI.

---

## 7. The single biggest risk

**"It's staged / faked / cherry-picked" — the reflex of the exact crowd that decides dev virality.** The film's beauty *creates* this suspicion: the more polished it looks, the less the jaded engineer believes the terminal is real. If that accusation gets oxygen before your proof does, the launch dies in the replies.

**How to avoid it:**
1. **Shoot the central proof as one continuous take with the streamed TUI picture-in-picture'd into the framed shot** — never a hard cut from "hands still" to "full-screen capture." The cut is precisely where belief breaks.
2. **Record and pin the raw, unedited, no-music end-to-end session as reply #1** on every launch post and link it at the top of the README. The film earns the share; the receipt converts it into a star.
3. **Never fake a single frame** — no invented test counts, no fictional cost meters, only verified line/commit numbers. The OSS teardown showing real, scarred code (`// send-keys Backspace types the word "Backspace"`) is your strongest inoculation: a person with nothing to hide shows the source.
4. **Name a verifiable artifact** in the caption (a commit hash, a PR number) and invite people to check. Turning the skeptics into your fact-checkers is the same move that turns them into your amplifiers.
