"""Projects (spec §5.2): registered rows ∪ discovered folders under the allowed
roots. Every root that reaches the store went through session_manager.
resolve_project_path — the sandbox is not re-implemented here."""
from __future__ import annotations

import os

import session_manager
from yuri.domain.event import EventType, YuriEvent
from yuri.domain.project import Project, slugify
from yuri.events.bus import EventBus
from yuri.home import Home
from yuri.store.base import Store

HOME_SLUG = "yuri"


class ProjectService:
    def __init__(self, store: Store, home: Home, bus: EventBus):
        self.store = store
        self.home_dir = home
        self.bus = bus

    def _unique_slug(self, base: str) -> str:
        slug, i = base, 2
        while self.store.projects.get_by_slug(slug) is not None:
            slug = f"{base}-{i}"
            i += 1
        return slug

    def ensure_home(self) -> Project:
        root = os.path.realpath(self.home_dir.path)
        existing = self.store.projects.get_by_root(root)
        if existing:
            return existing
        p = Project(slug=self._unique_slug(HOME_SLUG), name=os.path.basename(root) or "Yuri",
                    root_path=root, kind="home", auto_approve_edits=True)
        self.store.projects.insert(p)
        self.bus.publish(YuriEvent.make(EventType.PROJECT_REGISTERED, project_id=p.id,
                                        payload={"name": p.name, "root_path": root, "kind": "home"}))
        return p

    def home(self) -> Project:
        return self.ensure_home()

    def registered(self) -> list[Project]:
        """The registered project rows — as opposed to `list()`, which is those
        rows ∪ the folders discovered under the allowed roots. Lets the API layer
        read projects without reaching into the store (spec §5.8)."""
        return self.store.projects.list()

    def get(self, project_id: str) -> Project:
        p = self.store.projects.get(project_id)
        if p is None:
            raise KeyError(f"unknown project: {project_id}")
        return p

    def resolve_or_create(self, ref: str) -> Project:
        root = session_manager.resolve_project_path(ref)   # raises ValueError (sandbox)
        return self._upsert(root, None, None)

    def register(self, path: str, name: str | None = None, default_agent: str | None = None) -> Project:
        root = session_manager.resolve_project_path(path)
        return self._upsert(root, name, default_agent)

    def _upsert(self, root: str, name: str | None, default_agent: str | None) -> Project:
        existing = self.store.projects.get_by_root(root)
        if existing:
            changed = False
            if name and existing.name != name:
                existing.name, changed = name, True
            if default_agent and existing.default_agent != default_agent:
                existing.default_agent, changed = default_agent, True
            if changed:
                self.store.projects.update(existing)
            return existing
        name = name or os.path.basename(root) or "project"
        kind = "home" if root == os.path.realpath(self.home_dir.path) else "user"
        p = Project(slug=self._unique_slug(slugify(name)), name=name, root_path=root, kind=kind,
                    default_agent=default_agent, auto_approve_edits=(kind == "home"))
        self.store.projects.insert(p)
        self.bus.publish(YuriEvent.make(EventType.PROJECT_REGISTERED, project_id=p.id,
                                        payload={"name": p.name, "root_path": root, "kind": kind}))
        return p

    # The keys a project's verify config may carry — one per command-running
    # check in services/verify.py. An unknown key is refused rather than
    # stored: a typo'd "test" would sit in the row looking configured while
    # tests_pass went on reporting `unavailable`, which is the confusing
    # version of a correct refusal.
    VERIFY_KEYS = ("tests", "typecheck")

    def set_verify(self, project_id: str, config: dict) -> Project:
        """Set how this project's tests and typecheck are run.

        Verification refuses to claim a check it did not run, and refuses to
        guess the command, so this is the only thing that makes tests_pass
        answerable at all. Without it every bug-fix workflow blocks at its
        test task — correctly, but permanently.
        """
        p = self.get(project_id)
        clean: dict[str, str] = {}
        for key, value in (config or {}).items():
            if key not in self.VERIFY_KEYS:
                raise ValueError(
                    f"unknown verify key: {key!r}; expected one of {list(self.VERIFY_KEYS)}")
            text = " ".join(str(value).split())
            if not text:
                continue          # empty means "unset this one", not "run nothing"
            clean[key] = text
        meta = dict(p.metadata or {})
        if clean:
            meta["verify"] = clean
        else:
            meta.pop("verify", None)
        p.metadata = meta
        self.store.projects.update(p)
        return p

    def list(self) -> dict:
        discovered = session_manager.list_projects()
        registered = {p.root_path: p for p in self.store.projects.list()}
        home_root = os.path.realpath(self.home_dir.path)
        out: list[dict] = []
        seen: set[str] = set()
        for p in registered.values():
            seen.add(p.root_path)
            out.append({"name": p.name, "path": p.root_path, "registered": True, "id": p.id,
                        "slug": p.slug, "kind": p.kind, "default_agent": p.default_agent})
        for d in discovered["projects"]:
            real = os.path.realpath(d["path"])
            if real in seen:
                continue
            # Yuri's home is itself an allowed root (config.allowed_project_roots),
            # so session_manager.list_projects() also lists its internals
            # (memory/journal/workspace) as "discovered" folders. They are Yuri's
            # own state, never a coding project, and the home project itself
            # already comes through the registered branch above — so skip any
            # unregistered entry that lives directly inside the home root.
            if os.path.dirname(real) == home_root:
                continue
            out.append({"name": d["name"], "path": d["path"], "registered": False})
        out.sort(key=lambda x: x["name"].lower())
        return {"roots": discovered["roots"], "projects": out}
