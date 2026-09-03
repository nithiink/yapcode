"""RosterService — the roster of named specialists (spec §5, §6).

A `Specialist` is what a workflow task actually gets dispatched to: this
service is CRUD plus the two things that make roles usable at all --

  seed()      puts the six BUILTINS (domain/specialist.py) into a fresh
              store, remapped onto a provider the user actually runs.
  candidates()/resolve()   turn a task's `role` + `requires` into an actual
              specialist, deterministically and with no model call --
              §7.9's routing has to be testable and replayable, not a guess.

No task may create tasks and no provider talks to this service directly (see
the plan's §Global Constraints); it is reached only through WorkflowEngine and
the API/voice-tool layer above it.
"""
from __future__ import annotations

from yuri.domain.event import EventType, YuriEvent
from yuri.domain.ids import utcnow
from yuri.domain.specialist import BUILTINS, ROLE_PREFERENCE, Specialist
from yuri.events.bus import EventBus
from yuri.providers.registry import AgentRegistry
from yuri.store.base import Store

# Fields update() is allowed to touch. NEVER slug/id/builtin/created_at: a
# running session was launched holding the old slug, and builtin/created_at
# describe the row's origin, not its content. "archived" is deliberately
# absent too -- archive() is the only path that may flip it, because it is
# the one that checks SpecialistInUse first.
_UPDATABLE = ("name", "role", "provider_id", "description", "system_prompt", "model",
              "tools", "permission_mode", "capabilities", "color")


class SpecialistInUse(Exception):
    """Raised by archive(): the specialist is a builtin, or a live (non-
    terminal) task still names it. Archiving out from under a running task
    would leave nothing to record who is doing the work."""


class NoSpecialist(Exception):
    """Raised by resolve() when no specialist can take the work -- including
    a pin that cannot cover it. The message always names the role, the
    capabilities that were missing, and the fix, because this exception is
    the failure Yuri has to say out loud to the user, not just log."""


class DuplicateSpecialist(ValueError):
    """A LIVE specialist already holds this name. Archived rows free their
    name (spec §7.1's specialists_name_live partial index), so this can never
    fire for a name that was only ever used by something now archived."""


class RosterService:
    def __init__(self, store: Store, bus: EventBus, registry: AgentRegistry):
        self.store = store
        self.bus = bus
        self.registry = registry

    # --- seeding ----------------------------------------------------------
    def seed(self) -> int:
        """Insert whichever of the six BUILTINS is missing by name, and
        return how many were inserted. Idempotent: get_by_name is the guard,
        so a second call touches nothing.

        BUILTINS name claude-code/opencode as their `provider_id` (spec
        §7.32's defaults), but the user may run neither. A roster pointing at
        a provider that isn't registered is a roster of broken buttons, so an
        unregistered declared provider is remapped to one that IS registered
        -- preferring a provider whose AgentCapabilities.supports_personas is
        True (so the specialist's prompt reaches the agent as a real persona,
        not a degraded prepend), then any registered provider at all. If
        NOTHING is registered there is no safe remap, so this raises rather
        than inserting a specialist that can never be dispatched.
        """
        if not self.registry.ids():
            raise ValueError(
                "no agent provider is registered; configure one (YURI_AGENTS) "
                "before seeding the roster")
        inserted = 0
        for b in BUILTINS:
            if self.store.specialists.get_by_name(b["name"]) is not None:
                continue
            fields = dict(b)
            if fields["provider_id"] not in self.registry.ids():
                fields["provider_id"] = self._fallback_provider()
            s = Specialist(builtin=True, **fields)
            self.store.specialists.insert(s)
            self.bus.publish(YuriEvent.make(EventType.SPECIALIST_CREATED, payload={
                "id": s.id, "name": s.name, "role": s.role,
                "provider_id": s.provider_id, "builtin": True}))
            inserted += 1
        return inserted

    def _fallback_provider(self) -> str:
        for p in self.registry.all():
            if p.capabilities().supports_personas:
                return p.id
        return self.registry.ids()[0]

    def _check_provider(self, provider_id: str) -> None:
        ids = self.registry.ids()
        if provider_id not in ids:
            raise ValueError(
                f"unknown provider: {provider_id!r}; available: "
                f"{', '.join(ids) if ids else 'none configured'}")

    # --- reads --------------------------------------------------------
    def get(self, id: str) -> Specialist:
        s = self.store.specialists.get(id)
        if s is None:
            raise KeyError(f"unknown specialist: {id}")
        return s

    def by_name(self, name: str) -> Specialist | None:
        return self.store.specialists.get_by_name(name)

    def list(self, include_archived: bool = False) -> list[Specialist]:
        return self.store.specialists.list(include_archived=include_archived)

    # --- writes -------------------------------------------------------
    def create(self, **fields) -> Specialist:
        # A caller can only ever make a user specialist; these are derived or
        # fixed at creation and would otherwise let a caller forge a builtin
        # or collide an id/slug it does not own.
        for locked in ("id", "slug", "builtin", "created_at", "updated_at"):
            fields.pop(locked, None)
        provider_id = fields.get("provider_id")
        if provider_id is not None:
            self._check_provider(provider_id)
        # Construct first: Specialist.__post_init__ validates role/capabilities
        # and derives the slug, so the DuplicateSpecialist check below runs
        # against the name that will actually be stored.
        s = Specialist(**fields)
        if self.store.specialists.get_by_name(s.name) is not None:
            raise DuplicateSpecialist(f"a specialist named {s.name!r} already exists")
        self.store.specialists.insert(s)
        self.bus.publish(YuriEvent.make(EventType.SPECIALIST_CREATED, payload={
            "id": s.id, "name": s.name, "role": s.role, "provider_id": s.provider_id}))
        return s

    def update(self, id: str, **fields) -> Specialist:
        s = self.get(id)
        for locked in ("slug", "id", "builtin", "created_at"):
            fields.pop(locked, None)
        if "provider_id" in fields:
            self._check_provider(fields["provider_id"])
        new_name = fields.get("name")
        if (new_name and new_name != s.name
                and self.store.specialists.get_by_name(new_name) is not None):
            raise DuplicateSpecialist(f"a specialist named {new_name!r} already exists")
        for k in _UPDATABLE:
            if k in fields:
                setattr(s, k, fields[k])
        # Re-run the dataclass's own validation/coercion (role, capabilities,
        # tools/capabilities -> tuple) instead of duplicating it here. Safe to
        # call twice: slug is only derived `if not self.slug`, and it is
        # already set, so this can never move it.
        s.__post_init__()
        s.updated_at = utcnow()
        self.store.specialists.update(s)
        self.bus.publish(YuriEvent.make(EventType.SPECIALIST_UPDATED, payload={
            "id": s.id, "name": s.name, "role": s.role, "provider_id": s.provider_id}))
        return s

    def archive(self, id: str) -> None:
        s = self.get(id)
        if s.builtin:
            raise SpecialistInUse(
                f"'{s.name}' is a built-in specialist and cannot be archived")
        holders = self.store.tasks.holders_of(s.id, live_only=True)
        if holders:
            raise SpecialistInUse(
                f"'{s.name}' is holding {len(holders)} live task(s); finish, cancel, "
                "or reassign them before archiving")
        s.archived = True
        s.updated_at = utcnow()
        self.store.specialists.update(s)
        self.bus.publish(YuriEvent.make(EventType.SPECIALIST_ARCHIVED,
                                        payload={"id": s.id, "name": s.name}))

    # --- routing (spec §6) ---------------------------------------------
    def candidates(self, role: str, requires: frozenset[str] = frozenset()) -> list[Specialist]:
        """Live specialists that can take a `role` task needing `requires`,
        best first. NEVER raises: an empty list is a real answer the caller
        has to say out loud, not a crash.

        Filter: when `requires` is given it is a hard gate -- a specialist
        whose role matches but is missing a required capability cannot
        actually do the work, so it is excluded regardless of role. With no
        `requires` (the common "who can be a reviewer" query), every
        specialist trivially satisfies the empty set, so the filter falls
        back to an exact role match instead of returning the entire roster.

        Order (best first): exact role match, then capability superset, then
        the role's preferred provider (ROLE_PREFERENCE, spec §7.32), then most
        recently updated. The provider preference only ORDERS — it never
        excludes, because a user who created a reviewer on the other provider
        meant it.
        Applied as four STABLE sorts from least to most significant -- Python's
        sort is stable, so composing them this way avoids having to invert an
        ascending/descending direction inside one composite key.
        """
        requires = frozenset(requires)
        # A store failure is NOT "nobody is available". Swallowing it here
        # would make resolve() tell the user to create a specialist when the
        # truth is that the database is unreadable — the same failure mode as
        # a view rendering a failed fetch as an empty list. "Never raises"
        # means no-matches is a legal answer, not that infrastructure errors
        # are hidden.
        pool = self.store.specialists.list()

        def covers(s: Specialist) -> bool:
            return requires <= set(s.capabilities)

        if requires:
            pool = [s for s in pool if covers(s)]
        else:
            pool = [s for s in pool if s.role == role]

        preferred = ROLE_PREFERENCE.get(role)
        pool.sort(key=lambda s: s.updated_at, reverse=True)      # 4th: most recent
        pool.sort(key=lambda s: 0 if s.provider_id == preferred else 1)   # 3rd: §7.32
        pool.sort(key=lambda s: 0 if covers(s) else 1)           # 2nd: capability superset
        pool.sort(key=lambda s: 0 if s.role == role else 1)      # 1st: exact role match
        return pool

    def resolve(self, role: str, requires: frozenset[str] = frozenset(),
               pinned: str | None = None) -> Specialist:
        """Pick the specialist a task actually dispatches to.

        A `pinned` id is a user choice, not a suggestion: it is validated
        (must exist, be live, and cover `requires`) and used as-is, or this
        raises. Silently substituting someone else would run the task on an
        agent the user did not choose -- worse than failing loudly. With no
        pin, the best of candidates() wins; an empty candidate list raises
        the same way, so every caller has exactly one failure mode to handle.
        """
        requires = frozenset(requires)
        if pinned is not None:
            s = self.store.specialists.get(pinned)
            if s is None:
                raise NoSpecialist(
                    f"the specialist pinned for this {role!r} task no longer exists; "
                    "pick another in the Agents view, or drop the requirement")
            if s.archived:
                raise NoSpecialist(
                    f"'{s.name}', pinned for this {role!r} task, is archived; "
                    "pick another in the Agents view, or drop the requirement")
            missing = requires - set(s.capabilities)
            if missing:
                raise NoSpecialist(
                    f"'{s.name}' is pinned for this {role!r} task but lacks "
                    f"{', '.join(sorted(missing))}; pick another specialist in the "
                    "Agents view, or drop the requirement")
            return s
        got = self.candidates(role, requires)
        if not got:
            needs = f" requiring {', '.join(sorted(requires))}" if requires else ""
            raise NoSpecialist(
                f"no specialist can take a {role!r} task{needs}; "
                "create one in the Agents view, or drop the requirement")
        return got[0]
