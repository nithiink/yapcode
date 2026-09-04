import test from "node:test";
import assert from "node:assert/strict";
import {
  EMPTY_FORM, MAX_SERVERS, TIER_CHOICES, canSave, canTest, fingerprint,
  formProblem, parseArgs, parseEnv, requestBody, rowActions, slug,
  verdictSummary, type ServerForm, type TestResult,
} from "./mcp.ts";

const form = (over: Partial<ServerForm> = {}): ServerForm => ({
  ...EMPTY_FORM, name: "weather", command: "uvx", args: "mcp-weather", tier: "safe", ...over,
});

test("a complete form can be tested", () => {
  assert.equal(formProblem(form()), "");
  assert.ok(canTest(form()));
});

test("the tier must be chosen and has no default anywhere", () => {
  // A default tier is a security decision made by whoever left the field
  // alone. Same rule as the config file and the API.
  assert.equal(EMPTY_FORM.tier, null);
  assert.match(formProblem(form({ tier: null })), /ask before using it/);
  assert.ok(!canTest(form({ tier: null })));
});

test("the tier choices say what they mean, not what they are called", () => {
  for (const c of TIER_CHOICES) {
    assert.ok(!/^(safe|confirm)$/i.test(c.label), `${c.value} is labelled with jargon`);
    assert.ok(c.detail.length > 20, `${c.value} has no explanation`);
  }
});

test("a stdio server needs a command", () => {
  assert.match(formProblem(form({ command: "  " })), /which command/);
});

test("a name is required, and must contain something usable", () => {
  assert.match(formProblem(form({ name: "" })), /Give the service a name/);
  assert.match(formProblem(form({ name: "!!!" })), /no letters or numbers/);
});

test("a name that collides with an existing service is refused", () => {
  // Including one that only collides AFTER slugging — otherwise the backend
  // would silently rename the user's server or shadow the other one.
  assert.match(formProblem(form({ name: "My Weather" }), ["my-weather"]), /already have/);
});

test("the server limit is reported before the user fills the form in", () => {
  const many = Array.from({ length: MAX_SERVERS }, (_, i) => `s${i}`);
  assert.match(formProblem(form({ name: "one-more" }), many), /which is the limit/);
});

test("an unbuilt transport is refused rather than sent", () => {
  assert.match(formProblem(form({ transport: "http" as never })), /isn't supported yet/);
});

test("slugging matches the backend's rules", () => {
  assert.equal(slug("My Weather!"), "my-weather");
  assert.equal(slug("  spaced  out  "), "spaced-out");
  assert.equal(slug("!!!"), "");
  assert.equal(slug("x".repeat(80)).length, 64);
});

test("args split on whitespace and drop the empties", () => {
  assert.deepEqual(parseArgs("  mcp-weather   --verbose "), ["mcp-weather", "--verbose"]);
  assert.deepEqual(parseArgs(""), []);
});

test("env is KEY=value per line, and a line that isn't is reported", () => {
  // A mistyped key that silently vanishes is the failure the user would spend
  // the longest not understanding.
  const { env, bad } = parseEnv("A=1\n# a comment\n\nB = two \nnonsense");
  assert.deepEqual(env, { A: "1", B: "two" });
  assert.deepEqual(bad, ["nonsense"]);
  assert.match(formProblem(form({ env: "nonsense" })), /isn't KEY=value/);
});

test("a value containing = survives intact", () => {
  const { env } = parseEnv("TOKEN=abc=def==");
  assert.equal(env.TOKEN, "abc=def==");
});

const ok: TestResult = { verdict: "ok", server_name: "weather-mcp", server_version: "1.2",
                         tools: [{ name: "forecast" }] };

test("save is impossible until a test has answered", () => {
  // THE rule the flow exists for.
  assert.ok(!canSave(form(), [], null, null));
  assert.ok(canSave(form(), [], ok, fingerprint(form())));
});

test("a failed test can never be saved", () => {
  const bad: TestResult = { verdict: "failed", error: "command not found" };
  assert.ok(!canSave(form(), [], bad, fingerprint(form())));
});

test("an empty result may be saved, because it connected", () => {
  assert.ok(canSave(form(), [], { verdict: "empty" }, fingerprint(form())));
});

test("editing the command after a green test retracts permission to save", () => {
  // Otherwise the user saves something that was never checked.
  const tested = fingerprint(form());
  assert.ok(!canSave(form({ command: "npx" }), [], ok, tested));
  assert.ok(!canSave(form({ args: "other-package" }), [], ok, tested));
  assert.ok(!canSave(form({ env: "KEY=new" }), [], ok, tested));
});

test("changing only the tier keeps a passing test", () => {
  // The tier changes how a tool is gated, not whether the server starts.
  const tested = fingerprint(form({ tier: "safe" }));
  assert.ok(canSave(form({ tier: "confirm" }), [], ok, tested));
});

test("the request body carries the slug, not the typed name", () => {
  const body = requestBody(form({ name: "My Weather", env: "K=v" }));
  assert.deepEqual(body, { name: "my-weather", transport: "stdio", tier: "safe",
                          command: "uvx", args: ["mcp-weather"], env: { K: "v" } });
});

test("an ok verdict names the server itself, so the user can confirm it", () => {
  const s = verdictSummary(ok);
  assert.equal(s.tone, "good");
  assert.match(s.text, /weather-mcp 1\.2/);
  assert.match(s.text, /1 tool\b/);
});

test("empty reads as a warning, never as a pass", () => {
  const s = verdictSummary({ verdict: "empty" });
  assert.equal(s.tone, "warn");
  assert.match(s.text, /adds nothing/);
});

test("a failure shows the reason AND the stderr", () => {
  // Without the stderr the user has nothing to act on.
  const s = verdictSummary({ verdict: "failed", error: "exited 1",
                             stderr: "fatal: MISSING_API_KEY is not set" });
  assert.equal(s.tone, "bad");
  assert.match(s.text, /exited 1/);
  assert.match(s.text, /MISSING_API_KEY/);
});

test("a failure with nothing to say still says something", () => {
  assert.match(verdictSummary({ verdict: "failed" }).text, /didn't start/);
});

test("only a row that could act shows the action", () => {
  // A control that would fail is not rendered.
  assert.deepEqual(rowActions({ name: "a", status: "connected" }),
                   { reconnect: false, toggle: "disable" });
  assert.deepEqual(rowActions({ name: "a", status: "failed" }),
                   { reconnect: true, toggle: "disable" });
  assert.deepEqual(rowActions({ name: "a", status: "disabled" }),
                   { reconnect: false, toggle: "enable" });
});
