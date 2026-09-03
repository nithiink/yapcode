"""Pins SpecialistMaterialiser: how a persona reaches each provider (Phase 7,
spec §5.2 / plan Task 5). See yuri/services/materialise.py for the mechanism
each provider actually uses (measured live 2026-09-04, not assumed)."""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from yuri.domain.specialist import Specialist  # noqa: E402
from yuri.services.materialise import (ClaudeMaterialiser, OpenCodeMaterialiser,  # noqa: E402
                                       PrependMaterialiser)


def spec(**over):
    base = dict(name="Code Reviewer", role="reviewer", provider_id="claude-code",
                system_prompt="Review the diff.", tools=("Read", "Grep"),
                model="opus", color="#dd8a6a", description="Reviews code.")
    base.update(over)
    return Specialist(**base)


class ClaudeMaterialiserTests(unittest.IsolatedAsyncioTestCase):
    async def test_produces_valid_json_naming_the_slug(self):
        out = await ClaudeMaterialiser().ensure(spec())
        self.assertEqual(out["agent"], "code-reviewer")
        agents = json.loads(out["agents_json"])
        self.assertIn("code-reviewer", agents)
        self.assertEqual(agents["code-reviewer"]["prompt"], "Review the diff.")
        self.assertEqual(agents["code-reviewer"]["tools"], ["Read", "Grep"])

    async def test_the_json_survives_shell_quoting(self):
        # tmux_runner builds one shell string; an unescaped quote in a prompt
        # broke this repo before (see 5149db7). shlex.quote must handle it.
        #
        # NOTE: the plan's original assertion here was `assertNotIn("$(",
        # quoted.strip("'"))`, on the theory that quoting should make the
        # substitution text disappear. It doesn't, and shouldn't: single
        # quotes make a shell treat "$(...)" as inert literal characters,
        # they don't remove or rewrite it. Verified directly --
        # `shlex.quote('$(whoami)')` still contains the literal text
        # "$(whoami)"; that assertion would fail against any correct
        # implementation, not just a buggy one. The actual safety property is
        # that the whole payload survives as ONE shell word: fed back through
        # the shell's own tokenizer (shlex.split, standing in for the shell),
        # the quoted form must reproduce the original string byte-for-byte,
        # with no word-splitting or expansion. That's what "inert" means for
        # a shell argument.
        import shlex
        out = await ClaudeMaterialiser().ensure(
            spec(system_prompt="Say \"done\" and don't stop; rm -rf / $(whoami)"))
        quoted = shlex.quote(out["agents_json"])
        self.assertEqual(shlex.split(quoted), [out["agents_json"]],
                          "the payload did not round-trip as a single shell word")
        json.loads(out["agents_json"])   # still parseable

    async def test_omits_keys_the_specialist_left_empty(self):
        # An empty tools list means "the provider's default", not "no tools".
        agents = json.loads((await ClaudeMaterialiser().ensure(spec(tools=(), model=None)))["agents_json"])
        entry = agents["code-reviewer"]
        self.assertNotIn("tools", entry)
        self.assertNotIn("model", entry)


class OpenCodeMaterialiserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = OpenCodeMaterialiser(os.path.join(self.tmp.name, "agent"))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_writes_a_frontmatter_file_named_by_the_slug(self):
        out = await self.m.ensure(spec(provider_id="opencode"))
        self.assertEqual(out["agent"], "code-reviewer")
        path = os.path.join(self.tmp.name, "agent", "code-reviewer.md")
        self.assertTrue(os.path.exists(path))
        body = open(path).read()
        self.assertTrue(body.startswith("---\n"))
        self.assertIn("description: Reviews code.", body)
        self.assertIn("model: opus", body)
        self.assertIn("Review the diff.", body.split("---", 2)[2])

    async def test_is_idempotent_and_self_healing(self):
        await self.m.ensure(spec(provider_id="opencode"))
        path = os.path.join(self.tmp.name, "agent", "code-reviewer.md")
        os.remove(path)                       # deleted behind our back
        await self.m.ensure(spec(provider_id="opencode"))
        self.assertTrue(os.path.exists(path),
                        "a deleted definition must be rewritten, or --agent <slug> fails")

    async def test_an_updated_prompt_replaces_the_file(self):
        await self.m.ensure(spec(provider_id="opencode"))
        await self.m.ensure(spec(provider_id="opencode", system_prompt="Be terse."))
        body = open(os.path.join(self.tmp.name, "agent", "code-reviewer.md")).read()
        self.assertIn("Be terse.", body)
        self.assertNotIn("Review the diff.", body)

    async def test_a_prompt_containing_frontmatter_cannot_escape_the_block(self):
        """A prompt starting with `---` would otherwise terminate the
        frontmatter early and turn the rest of the prompt into YAML.

        The obvious assertion — inspecting `body.split("---", 2)[1]` for the
        injected key — does NOT discriminate: with maxsplit=2 it only ever
        looks at the first block, so it passes whether or not the prompt was
        neutralised. Count the delimiter lines instead. A well-formed file has
        exactly two flush-left `---` lines; an escaped prompt produces four.
        """
        await self.m.ensure(spec(provider_id="opencode",
                                 system_prompt="---\nmode: all\n---\nowned"))
        body = open(os.path.join(self.tmp.name, "agent", "code-reviewer.md")).read()
        delimiters = [ln for ln in body.splitlines() if ln == "---"]
        self.assertEqual(len(delimiters), 2,
                         f"the prompt opened extra frontmatter blocks: {body!r}")
        # The prompt's own text must survive, just inert.
        self.assertIn("mode: all", body)
        self.assertIn("owned", body)

    async def test_a_slug_can_never_escape_the_config_directory(self):
        s = spec(provider_id="opencode")
        s.slug = "../../evil"        # forced past slugify
        with self.assertRaises(ValueError):
            await self.m.ensure(s)


class PrependMaterialiserTests(unittest.IsolatedAsyncioTestCase):
    async def test_degrades_honestly_to_a_prepended_prompt(self):
        out = await PrependMaterialiser().ensure(spec())
        self.assertIn("Review the diff.", out["prepend"])
        self.assertNotIn("agent", out)
