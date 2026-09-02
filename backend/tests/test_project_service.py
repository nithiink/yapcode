import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.projects import ProjectService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class ProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "alpha"))
        self.home = Home(os.path.join(self.root, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", self.home.path)]
        [p.start() for p in self.patches]
        self.svc = ProjectService(self.store, self.home, self.bus)

    def tearDown(self):
        [p.stop() for p in self.patches]
        self.store.close()
        self.tmp.cleanup()

    def test_ensure_home_is_idempotent(self):
        h1 = self.svc.ensure_home()
        h2 = self.svc.ensure_home()
        self.assertEqual(h1.id, h2.id)
        self.assertEqual((h1.kind, h1.slug, h1.auto_approve_edits), ("home", "yuri", True))
        self.assertEqual(h1.root_path, os.path.realpath(self.home.path))
        self.assertEqual(self.svc.home().id, h1.id)

    def test_resolve_or_create_upserts_by_root(self):
        p1 = self.svc.resolve_or_create("alpha")
        p2 = self.svc.resolve_or_create(os.path.join(self.root, "alpha"))
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(p1.slug, "alpha")
        ev = self.events.get_nowait()
        self.assertEqual(ev.type, "project.registered")
        self.assertEqual(ev.project_id, p1.id)

    def test_resolve_bad_ref_raises(self):
        with self.assertRaises(ValueError):
            self.svc.resolve_or_create("/etc")

    def test_register_and_slug_dedupe(self):
        os.mkdir(os.path.join(self.root, "Alpha2"))
        a = self.svc.register(os.path.join(self.root, "alpha"), name="Alpha")
        b = self.svc.register(os.path.join(self.root, "Alpha2"), name="Alpha", default_agent="claude-code")
        self.assertEqual(a.slug, "alpha")
        self.assertEqual(b.slug, "alpha-2")
        self.assertEqual(b.default_agent, "claude-code")

    def test_list_merges_registered_and_discovered(self):
        self.svc.ensure_home()
        self.svc.resolve_or_create("alpha")
        os.mkdir(os.path.join(self.root, "beta"))
        out = self.svc.list()
        by_name = {p["name"]: p for p in out["projects"]}
        self.assertTrue(by_name["alpha"]["registered"])
        self.assertFalse(by_name["beta"]["registered"])
        self.assertEqual(by_name["Yuri"]["kind"], "home")
        self.assertIn(os.path.realpath(self.home.path), out["roots"])

    def test_list_excludes_yuris_own_internals(self):
        # Yuri's home is itself an allowed root (config.allowed_project_roots
        # appends it), so session_manager.list_projects() would otherwise also
        # discover memory/journal/workspace as unregistered "projects" — which
        # would let the voice model offer them as a place to start a coding
        # session. The home project itself must still appear (as a registered
        # row, via ensure_home()), just not its internal subfolders.
        self.svc.ensure_home()
        out = self.svc.list()
        names = {p["name"] for p in out["projects"]}
        self.assertIn("Yuri", names)
        self.assertEqual({p for p in ("memory", "journal", "workspace") if p in names}, set())
        by_name = {p["name"]: p for p in out["projects"]}
        self.assertEqual(by_name["Yuri"]["kind"], "home")
        self.assertTrue(by_name["Yuri"]["registered"])


if __name__ == "__main__":
    unittest.main()
