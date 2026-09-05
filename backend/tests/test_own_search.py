"""web_search (spec §2).

The parser is tested without a network on purpose: a shape change upstream
must degrade to "no answer" rather than raise inside a voice turn, and that is
only checkable against hand-written bodies.

    .venv/bin/python -m unittest tests.test_own_search -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.own.search import (ANSWER_MAX, SOURCES_MAX, SearchUnavailable,  # noqa: E402
                             parse_grounded, search)


def body(text: str, *, chunks=None, queries=None) -> dict:
    cand: dict = {"content": {"parts": [{"text": text}]}}
    meta: dict = {}
    if chunks is not None:
        meta["groundingChunks"] = chunks
    if queries is not None:
        meta["webSearchQueries"] = queries
    if meta:
        cand["groundingMetadata"] = meta
    return {"candidates": [cand]}


def web(title: str, uri: str = "https://example.com") -> dict:
    return {"web": {"title": title, "uri": uri}}


class ParsingTests(unittest.TestCase):
    def test_a_grounded_answer_carries_its_sources_and_the_query_it_ran(self):
        r = parse_grounded(body("Alcaraz won.",
                                chunks=[web("wikipedia.org"), web("ausopen.com")],
                                queries=["2026 Australian Open winner"]))
        self.assertEqual(r.answer, "Alcaraz won.")
        self.assertEqual([s["title"] for s in r.sources], ["wikipedia.org", "ausopen.com"])
        self.assertEqual(r.searched, ["2026 Australian Open winner"])
        self.assertTrue(r.to_dict()["grounded"])

    def test_an_answer_with_no_sources_is_marked_ungrounded(self):
        # The one thing this tool exists to prevent: an ungrounded answer is
        # indistinguishable from the model's own memory. She has to be able to
        # tell the user she could not find a source.
        r = parse_grounded(body("Probably Tuesday."))
        self.assertEqual(r.answer, "Probably Tuesday.")
        self.assertEqual(r.sources, [])
        self.assertFalse(r.to_dict()["grounded"])

    def test_sources_are_capped_and_deduped_by_title(self):
        # Three pages from one site must not become three spoken citations.
        chunks = [web("wikipedia.org", f"https://w/{i}") for i in range(3)]
        chunks += [web(f"site{i}.com") for i in range(6)]
        r = parse_grounded(body("x", chunks=chunks))
        self.assertLessEqual(len(r.sources), SOURCES_MAX)
        titles = [s["title"] for s in r.sources]
        self.assertEqual(len(titles), len(set(titles)))

    def test_a_long_answer_is_cut_at_a_sentence_and_marked(self):
        long = ("This is a full sentence. " * 60).strip()
        r = parse_grounded(body(long))
        self.assertLessEqual(len(r.answer), ANSWER_MAX)
        self.assertTrue(r.truncated)
        self.assertTrue(r.answer.endswith("."), f"cut mid-word: {r.answer[-40:]!r}")

    def test_an_answer_with_no_sentence_break_is_still_cut_and_marked(self):
        r = parse_grounded(body("a" * 900))
        self.assertLessEqual(len(r.answer), ANSWER_MAX)
        self.assertTrue(r.answer.endswith("…"), "silent truncation")

    def test_a_short_answer_is_untouched(self):
        r = parse_grounded(body("Yes."))
        self.assertEqual(r.answer, "Yes.")
        self.assertFalse(r.truncated)

    def test_whitespace_is_flattened_because_she_speaks_it(self):
        r = parse_grounded(body("Two\n\nlines   and\ttabs."))
        self.assertEqual(r.answer, "Two lines and tabs.")

    def test_every_malformed_shape_degrades_to_no_answer_rather_than_raising(self):
        for bad in [None, "a string", 42, {}, {"candidates": []}, {"candidates": "x"},
                    {"candidates": [None]}, {"candidates": [{}]},
                    {"candidates": [{"content": "not a dict"}]},
                    {"candidates": [{"content": {"parts": "not a list"}}]},
                    {"candidates": [{"content": {"parts": [None, 7]}}]},
                    {"candidates": [{"groundingMetadata": "nope"}]}]:
            r = parse_grounded(bad)
            self.assertEqual(r.answer, "", repr(bad))
            self.assertEqual(r.sources, [], repr(bad))

    def test_a_source_without_a_title_is_skipped_not_rendered_blank(self):
        r = parse_grounded(body("x", chunks=[{"web": {"uri": "https://x"}},
                                             {"web": {"title": "  "}},
                                             {"not_web": {}},
                                             web("real.com")]))
        self.assertEqual([s["title"] for s in r.sources], ["real.com"])


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_query_is_refused_before_any_request(self):
        with self.assertRaises(SearchUnavailable) as ctx:
            await search("   ", api_key="k")
        self.assertIn("search for", str(ctx.exception))

    async def test_a_missing_key_says_exactly_what_to_do(self):
        with self.assertRaises(SearchUnavailable) as ctx:
            await search("anything", api_key="")
        msg = str(ctx.exception)
        self.assertIn("GEMINI_API_KEY", msg)
        self.assertIn(".env", msg)

    async def test_the_upstream_body_is_never_relayed(self):
        # It can carry the key or a long trace, and this text is both spoken
        # aloud and written to the event log.
        import httpx
        from unittest import mock

        class Resp:
            status_code = 403
            text = "GEMINI_API_KEY=secret-value-do-not-leak trace..."

            def json(self):
                return {"error": {"message": "key secret-value-do-not-leak invalid"}}

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return Resp()

        with mock.patch.object(httpx, "AsyncClient", lambda **k: Client()):
            with self.assertRaises(SearchUnavailable) as ctx:
                await search("anything", api_key="k")
        msg = str(ctx.exception)
        self.assertIn("403", msg)
        self.assertNotIn("secret-value-do-not-leak", msg)

    async def test_a_timeout_says_how_long_it_waited(self):
        import httpx
        from unittest import mock

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise httpx.TimeoutException("slow")

        with mock.patch.object(httpx, "AsyncClient", lambda **k: Client()):
            with self.assertRaises(SearchUnavailable) as ctx:
                await search("anything", api_key="k", timeout=7)
        self.assertIn("7 seconds", str(ctx.exception))

    async def test_a_quota_refusal_is_its_own_message(self):
        import httpx
        from unittest import mock

        class Resp:
            status_code = 429
            def json(self): return {}

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return Resp()

        with mock.patch.object(httpx, "AsyncClient", lambda **k: Client()):
            with self.assertRaises(SearchUnavailable) as ctx:
                await search("anything", api_key="k")
        self.assertIn("quota", str(ctx.exception).lower())
