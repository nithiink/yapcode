// Adding an MCP server: the form's rules, and what a test verdict means.
//
// Pure so `node --test` can reach it — the panel keeps the state and the
// fetches, this decides what is valid and what may be saved.
//
// The one rule the whole flow exists for: a server that has not answered
// cannot be saved. An unreachable server in config becomes a startup error
// and a capability she quietly does not have, discovered later and attributed
// to nothing. The backend re-tests on save too (a disabled button is a label,
// not a lock); these rules are what stops the user getting that far.

/** Mirrors yuri/mcp/config.py's TRANSPORTS. Only stdio is built: the wire
 *  protocol was verified against a real stdio server, and the spec's rule is
 *  not to ship a transport nobody has driven. */
export const TRANSPORTS = ["stdio"] as const;
export type Transport = (typeof TRANSPORTS)[number];

export const MAX_SERVERS = 8;

/** No default, deliberately — the same rule the config file follows. A default
 *  tier would be a security decision made by whoever left the field alone. */
export type Tier = "safe" | "confirm";

/** What each tier means, in words, rather than the word itself. "confirm" is
 *  jargon; "ask me first" is the thing the user is actually choosing. */
export const TIER_CHOICES: { value: Tier; label: string; detail: string }[] = [
  {
    value: "confirm",
    label: "Ask me before running its tools",
    detail: "Yuri tells you what she is about to do and waits for you to agree.",
  },
  {
    value: "safe",
    label: "Run its tools without asking",
    detail: "For services that only read things, or that you trust completely.",
  },
];

export type ServerForm = {
  name: string;
  transport: Transport;
  command: string;
  args: string;          // one line, split on whitespace
  env: string;           // KEY=value per line
  tier: Tier | null;
};

export const EMPTY_FORM: ServerForm = {
  name: "", transport: "stdio", command: "", args: "", env: "", tier: null,
};

export type Verdict = "ok" | "empty" | "failed";

export type TestResult = {
  verdict: Verdict;
  error?: string;
  stderr?: string;
  server_name?: string;
  server_version?: string;
  tools?: { name: string; description?: string; tier?: string }[];
  dropped_tools?: number;
  colliding_tools?: string[];
};

/** The slug rules from yuri/mcp/naming.py, so the form rejects a name the
 *  backend would rewrite rather than silently renaming the user's server. */
export function slug(text: string): string {
  return (text || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 64);
}

export function parseArgs(text: string): string[] {
  return (text || "").trim().split(/\s+/).filter(Boolean);
}

/** `KEY=value` per line. A line with no `=` is not a silent no-op: it is
 *  reported, because a mistyped API key that vanishes is the failure the user
 *  would spend the longest not understanding. */
export function parseEnv(text: string): { env: Record<string, string>; bad: string[] } {
  const env: Record<string, string> = {};
  const bad: string[] = [];
  for (const raw of (text || "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    if (at <= 0) { bad.push(line); continue; }
    env[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return { env, bad };
}

/** Why the form cannot be tested yet, or "" when it can.
 *
 *  One message at a time, in the order the user fills the form in: a wall of
 *  every problem at once is harder to act on than the next thing to fix. */
export function formProblem(form: ServerForm, existing: string[] = []): string {
  const name = slug(form.name);
  if (!form.name.trim()) return "Give the service a name.";
  if (!name) return "That name has no letters or numbers in it.";
  if (existing.includes(name)) return `You already have a service called “${name}”.`;
  if (existing.length >= MAX_SERVERS) {
    return `You have ${existing.length} services connected, which is the limit.`;
  }
  if (!TRANSPORTS.includes(form.transport)) return "That kind of connection isn't supported yet.";
  if (!form.command.trim()) return "Say which command starts the service.";
  const { bad } = parseEnv(form.env);
  if (bad.length) return `This line isn't KEY=value: “${bad[0]}”.`;
  // Last, not first: it is the one choice with no default, so asking for it
  // before the form is otherwise complete would nag rather than inform.
  if (!form.tier) return "Choose whether Yuri should ask before using it.";
  return "";
}

export function canTest(form: ServerForm, existing: string[] = []): boolean {
  return formProblem(form, existing) === "";
}

/** Save is allowed only after a test that did not fail, and only for the form
 *  that was tested — editing the command after a green test must retract it,
 *  or the user saves something that was never checked. */
export function canSave(form: ServerForm, existing: string[], result: TestResult | null,
                        testedFingerprint: string | null): boolean {
  if (!canTest(form, existing)) return false;
  if (!result || result.verdict === "failed") return false;
  return fingerprint(form) === testedFingerprint;
}

/** Everything about a form that changes what the test proves. `tier` is
 *  excluded on purpose: it changes how a tool is GATED, not whether the
 *  server starts, so changing it should not throw away a passing test. */
export function fingerprint(form: ServerForm): string {
  return JSON.stringify([slug(form.name), form.transport, form.command.trim(),
                         parseArgs(form.args), parseEnv(form.env).env]);
}

export function requestBody(form: ServerForm): Record<string, unknown> {
  return {
    name: slug(form.name),
    transport: form.transport,
    tier: form.tier,
    command: form.command.trim(),
    args: parseArgs(form.args),
    env: parseEnv(form.env).env,
  };
}

/** What to tell the user about a verdict. Three outcomes, three remedies —
 *  and `empty` is a warning, never a pass dressed up as one. */
export function verdictSummary(r: TestResult): { tone: "good" | "warn" | "bad"; text: string } {
  if (r.verdict === "ok") {
    const n = r.tools?.length || 0;
    const who = r.server_name
      ? `${r.server_name}${r.server_version ? ` ${r.server_version}` : ""}`
      : "The service";
    return { tone: "good", text: `${who} answered, with ${n} tool${n === 1 ? "" : "s"}.` };
  }
  if (r.verdict === "empty") {
    return {
      tone: "warn",
      text: "It answered but offers no tools, so connecting it adds nothing yet.",
    };
  }
  // The reason, verbatim, including the stderr — "failed to connect" on its
  // own is a dead end, and this is the moment the user needs the detail.
  const detail = [r.error, (r.stderr || "").trim()].filter(Boolean).join("\n");
  return { tone: "bad", text: detail || "It didn't start, and said nothing about why." };
}

export type ServerRow = {
  name: string;
  status: "connected" | "failed" | "disabled";
  error?: string;
  tool_count?: number;
  tools?: string[];
  server_name?: string;
  server_version?: string;
  env_keys?: string[];
  enabled?: boolean;
  dropped_tools?: number;
  colliding_tools?: string[];
};

/** A control that would fail is not rendered (docs/yuri/design/GUIDE.md), so
 *  the row decides its own actions rather than the panel guessing. */
export function rowActions(row: ServerRow): { reconnect: boolean; toggle: "enable" | "disable" } {
  return {
    reconnect: row.status === "failed",
    toggle: row.status === "disabled" ? "enable" : "disable",
  };
}
