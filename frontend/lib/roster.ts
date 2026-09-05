// The roster form's rules, and how a specialist is described.
//
// Pure so `node --test` can reach it — the panel keeps the state and the
// fetches, this decides what is valid.
//
// Every rule here MIRRORS a backend rule, and the mirror is the risk: a
// preview that disagrees with what the backend stores is worse than no
// preview, because the user acts on it. roster.test.ts shares the slug cases
// with the backend's own table for exactly that reason.

/** yuri/domain/specialist.py's ROLES, in the same order. */
export const ROLES = ["researcher", "developer", "tester", "reviewer",
                      "verifier", "documenter"] as const;
export type Role = (typeof ROLES)[number];

/** What a role is FOR, in the user's words rather than the enum's. The panel
 *  shows these; "verifier" alone tells a first-time reader nothing. */
export const ROLE_BLURB: Record<Role, string> = {
  researcher: "Reads the code and reports what it finds. Changes nothing.",
  developer: "Makes the change.",
  tester: "Runs the tests and reports what actually failed.",
  reviewer: "Reads the diff and says what is wrong with it.",
  verifier: "Checks a claim against what the commands actually output.",
  documenter: "Writes the docs and comments.",
};

/** yuri/domain/specialist.py's TASK_CAPABILITIES. */
export const TASK_CAPABILITIES = ["coding", "code_review", "research", "testing",
                                  "terminal", "browser", "git", "docs"] as const;
export type Capability = (typeof TASK_CAPABILITIES)[number];

export const PERMISSION_MODES = ["default", "plan", "acceptEdits", "bypassPermissions"] as const;

/** Colours a specialist may take: the design system's tokens, resolved,
 *  because a specialist's colour is drawn on the timeline and a free-text
 *  hex would let a user pick something invisible on this background.
 *  docs/yuri/design/GUIDE.md §1 forbids a literal colour in a component;
 *  these are the token VALUES, offered as a palette rather than a text field. */
export const SPECIALIST_COLORS = ["#dd8a6a", "#93a6c9", "#9cc7a4", "#d8b07a",
                                  "#d98a8a", "#928c81"] as const;

const HEX = /^#[0-9a-f]{6}$/i;

/** Mirrors yuri/domain/specialist.py's slugify EXACTLY.
 *
 *  The slug becomes `--agent <slug>` on a command line and `<slug>.md` in
 *  OpenCode's config directory, so the backend refuses to let it be empty and
 *  falls back to a generated id. This returns "" for that case instead of
 *  inventing a different id: the form shows "a name will be generated" rather
 *  than a slug that is not the one the backend will store. */
export function slugPreview(name: string): string {
  return (name || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

export type SpecialistForm = {
  name: string;
  role: Role | "";
  provider_id: string;
  description: string;
  system_prompt: string;
  model: string;
  color: string;
  permission_mode: string;
  capabilities: Capability[];
};

export const EMPTY_SPECIALIST: SpecialistForm = {
  name: "", role: "", provider_id: "", description: "", system_prompt: "",
  model: "", color: SPECIALIST_COLORS[0], permission_mode: "default", capabilities: [],
};

export type FieldErrors = Partial<Record<keyof SpecialistForm, string>>;

/** Field-by-field, because a form that says only "invalid" makes the user
 *  hunt. Names match the form's keys so the panel can put each message under
 *  its own input. */
export function validateSpecialist(form: SpecialistForm, existingNames: string[] = []): FieldErrors {
  const errors: FieldErrors = {};
  const name = (form.name || "").trim();
  if (!name) errors.name = "Give this agent a name.";
  else if (existingNames.some((n) => n.toLowerCase() === name.toLowerCase())) {
    errors.name = "You already have an agent with that name.";
  }
  if (!form.role) errors.role = "Pick what this agent is for.";
  else if (!ROLES.includes(form.role as Role)) errors.role = "That isn't one of the roles.";
  if (!(form.provider_id || "").trim()) errors.provider_id = "Pick which engine runs it.";
  const bad = (form.capabilities || []).filter(
    (c) => !TASK_CAPABILITIES.includes(c as Capability));
  if (bad.length) errors.capabilities = `Not a capability: ${bad.join(", ")}.`;
  if (form.color && !HEX.test(form.color)) errors.color = "That isn't a colour.";
  if (form.permission_mode && !PERMISSION_MODES.includes(form.permission_mode as never)) {
    errors.permission_mode = "That isn't a permission mode.";
  }
  return errors;
}

export function canSaveSpecialist(form: SpecialistForm, existingNames: string[] = []): boolean {
  return Object.keys(validateSpecialist(form, existingNames)).length === 0;
}

export type Specialist = {
  id: string;
  name: string;
  slug?: string;
  role: string;
  provider_id: string;
  description?: string;
  system_prompt?: string;
  model?: string | null;
  color?: string;
  permission_mode?: string;
  capabilities?: string[];
  builtin?: boolean;
  archived?: boolean;
};

/** The body to send. Only what the user set: an empty string for `model`
 *  means "the provider's default", and sending "" would store an empty model
 *  name rather than clearing it. */
export function specialistBody(form: SpecialistForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: form.name.trim(),
    role: form.role,
    provider_id: form.provider_id,
    color: form.color,
    permission_mode: form.permission_mode,
    capabilities: form.capabilities,
  };
  for (const key of ["description", "system_prompt", "model"] as const) {
    const value = (form[key] || "").trim();
    if (value) body[key] = value;
  }
  return body;
}

export function formFrom(s: Specialist): SpecialistForm {
  return {
    name: s.name || "",
    role: (s.role || "") as Role | "",
    provider_id: s.provider_id || "",
    description: s.description || "",
    system_prompt: s.system_prompt || "",
    model: s.model || "",
    color: s.color || SPECIALIST_COLORS[0],
    permission_mode: s.permission_mode || "default",
    capabilities: (s.capabilities || []).filter(
      (c): c is Capability => TASK_CAPABILITIES.includes(c as Capability)),
  };
}

/** A control that would fail is not rendered (GUIDE.md §6). A builtin cannot
 *  be archived and its persona is not the user's to rewrite, so the panel
 *  asks the row rather than guessing. */
export function specialistActions(s: Specialist): { edit: boolean; archive: boolean } {
  return { edit: !s.builtin, archive: !s.builtin && !s.archived };
}
