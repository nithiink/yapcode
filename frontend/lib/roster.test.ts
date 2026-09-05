import test from "node:test";
import assert from "node:assert/strict";
import {
  EMPTY_SPECIALIST, PERMISSION_MODES, ROLES, ROLE_BLURB, SPECIALIST_COLORS,
  TASK_CAPABILITIES, canSaveSpecialist, formFrom, slugPreview, specialistActions,
  specialistBody, validateSpecialist, type SpecialistForm,
} from "./roster.ts";

const form = (over: Partial<SpecialistForm> = {}): SpecialistForm => ({
  ...EMPTY_SPECIALIST, name: "Code Reviewer", role: "reviewer", provider_id: "claude-code",
  ...over,
});

// The cases below are LIFTED from backend/tests/test_specialist_domain.py's
// SlugTests. The preview is a mirror of the backend's slugify, and a mirror
// that drifts is worse than no preview because the user acts on it.
test("slugPreview matches the backend's slugify, case for case", () => {
  assert.equal(slugPreview("Code Reviewer"), "code-reviewer");
  assert.equal(slugPreview("  Deep   Research  "), "deep-research");
  assert.equal(slugPreview("Ops/Deploy!"), "ops-deploy");
  assert.equal(slugPreview("Tester (fast)"), "tester-fast");
});

test("a name with nothing usable previews as empty, not as a guess", () => {
  // The backend falls back to a generated id. Inventing a different one here
  // would show the user a slug that is not the one that gets stored.
  for (const name of ["", "   ", "!!!", "..", "../../etc"]) {
    const slug = slugPreview(name);
    assert.ok(!slug.includes("/"), name);
    assert.ok(!slug.includes(".."), name);
  }
  assert.equal(slugPreview("!!!"), "");
});

test("the role and capability lists match the backend's", () => {
  // Two copies of an enum is the cost of a typed frontend; a test is what
  // keeps them in step.
  assert.deepEqual([...ROLES], ["researcher", "developer", "tester", "reviewer",
                                "verifier", "documenter"]);
  assert.deepEqual([...TASK_CAPABILITIES], ["coding", "code_review", "research", "testing",
                                            "terminal", "browser", "git", "docs"]);
});

test("every role says what it is for", () => {
  // "verifier" alone tells a first-time reader nothing.
  for (const role of ROLES) {
    assert.ok(ROLE_BLURB[role]?.length > 15, role);
  }
});

test("a complete form is valid", () => {
  assert.deepEqual(validateSpecialist(form()), {});
  assert.ok(canSaveSpecialist(form()));
});

test("each missing field is reported under its own name", () => {
  // A form that says only "invalid" makes the user hunt.
  const errors = validateSpecialist({ ...EMPTY_SPECIALIST });
  assert.ok(errors.name);
  assert.ok(errors.role);
  assert.ok(errors.provider_id);
  assert.ok(!errors.capabilities, "an empty capability list is fine");
});

test("a duplicate name is refused, case-insensitively", () => {
  const errors = validateSpecialist(form({ name: "code reviewer" }), ["Code Reviewer"]);
  assert.match(errors.name!, /already have/);
});

test("an unknown role, capability, colour or permission mode is refused", () => {
  assert.ok(validateSpecialist(form({ role: "wizard" as never })).role);
  assert.ok(validateSpecialist(form({ capabilities: ["flying" as never] })).capabilities);
  assert.ok(validateSpecialist(form({ color: "not-a-colour" })).color);
  assert.ok(validateSpecialist(form({ permission_mode: "yolo" })).permission_mode);
});

test("the offered colours are all real hex the validator accepts", () => {
  for (const color of SPECIALIST_COLORS) {
    assert.deepEqual(validateSpecialist(form({ color })), {}, color);
  }
});

test("every permission mode the picker offers passes validation", () => {
  for (const permission_mode of PERMISSION_MODES) {
    assert.deepEqual(validateSpecialist(form({ permission_mode })), {}, permission_mode);
  }
});

test("the body omits an empty model rather than storing one", () => {
  // "" would store an empty model name; omitting it means the provider's
  // default, which is what the blank field means.
  const body = specialistBody(form({ model: "  ", description: "mine" }));
  assert.ok(!("model" in body));
  assert.equal(body.description, "mine");
  assert.equal(body.name, "Code Reviewer");
});

test("the body trims the name, because the slug would differ otherwise", () => {
  assert.equal(specialistBody(form({ name: "  Spaced  " })).name, "Spaced");
});

test("editing round-trips a specialist through the form", () => {
  const s = {
    id: "s1", name: "Reviewer", role: "reviewer", provider_id: "claude-code",
    model: null, capabilities: ["code_review", "not-a-real-one"], color: "#93a6c9",
  };
  const f = formFrom(s);
  assert.equal(f.name, "Reviewer");
  assert.equal(f.model, "");
  // An unknown capability from an older row is dropped rather than crashing
  // the form or being re-saved.
  assert.deepEqual(f.capabilities, ["code_review"]);
  assert.deepEqual(validateSpecialist(f), {});
});

test("a builtin offers neither edit nor archive", () => {
  // A control that would 409 is not rendered.
  assert.deepEqual(specialistActions({ id: "1", name: "Researcher", role: "researcher",
                                       provider_id: "opencode", builtin: true }),
                   { edit: false, archive: false });
  assert.deepEqual(specialistActions({ id: "2", name: "Mine", role: "reviewer",
                                       provider_id: "opencode" }),
                   { edit: true, archive: true });
  assert.deepEqual(specialistActions({ id: "3", name: "Old", role: "reviewer",
                                       provider_id: "opencode", archived: true }),
                   { edit: true, archive: false });
});
