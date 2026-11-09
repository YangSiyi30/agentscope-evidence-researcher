"""Observe real session state with deterministic backends, never an LLM score."""

import asyncio
import json
import unittest

from as_researcher.core import ResearchConfig, Source
from as_researcher.pages import PageReadError
from as_researcher.researcher import ResearchSession


def payload(response):
    return json.loads(response.content[0].text)


class BudgetObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_duplicate_report_attempts_not_remaining(self):
        calls = []

        async def search(query, limit):
            calls.append(query)
            return [Source("test", "https://example.org/", "snippet")]

        async def read(url, limit, **kwargs):
            # Reader-supplied metadata cannot replace local usage.
            return {"content": "page", "page_read_attempts": 99, "pages_read": 99}

        session = ResearchSession(ResearchConfig(max_searches=3, max_page_reads=2), search, read)
        first = payload(await session.search_web("test"))
        self.assertEqual((first["search_attempts"], first["remaining_searches"]), (1, 2))
        sid = first["sources"][0]["id"]
        page = payload(await session.read_page(sid))
        self.assertEqual(
            (page["page_read_attempts"], page["remaining_page_reads"], page["pages_read"]),
            (1, 1, 1),
        )
        duplicate = payload(await session.read_page(sid))
        self.assertEqual(session.trace[-1]["budget_after"], session._budget_observation())
        self.assertEqual(duplicate["error"], "already_attempted")
        self.assertEqual(duplicate["page_read_attempts"], 1)
        stopped = payload(await session.search_web(" TEST "))
        self.assertEqual(calls, ["test"])
        self.assertTrue(stopped["stop"])
        self.assertTrue(stopped["search_stopped"])
        self.assertEqual((stopped["search_attempts"], stopped["remaining_searches"]), (1, 2))
        self.assertEqual(stopped["pages_read"], len(session.page_evidence))
        for event in session.trace:
            self.assertIn("budget_after", event)
        self.assertEqual(session.trace[0]["budget_after"]["pages_read"], 0)
        self.assertEqual(session.trace[1]["budget_after"]["pages_read"], 1)

    async def test_failed_backends_consume_attempts_but_not_saved_pages(self):
        async def search(query, limit):
            raise RuntimeError("fixture failure")

        async def read(url, limit, **kwargs):
            raise PageReadError("fixture_failure")

        session = ResearchSession(ResearchConfig(max_searches=3, max_page_reads=2), search, read)
        first = payload(await session.search_web("test"))
        self.assertEqual((first["search_attempts"], first["remaining_searches"]), (1, 2))
        again = payload(await session.search_web("retry"))
        self.assertEqual(again["search_attempts"], 1)
        source = Source("known", "https://example.org/", "snippet")
        session.sources[source.url] = source
        failed = payload(await session.read_page(source.id))
        self.assertEqual(
            (failed["page_read_attempts"], failed["remaining_page_reads"], failed["pages_read"]),
            (1, 1, 0),
        )

    async def test_invalid_disabled_and_budget_blocked_calls_do_not_consume(self):
        async def forbidden(*args, **kwargs):
            self.fail("Backend must not be called")

        session = ResearchSession(ResearchConfig(max_page_reads=0), forbidden)
        blank = payload(await session.search_web(" "))
        self.assertEqual(blank["search_attempts"], 0)
        disabled = payload(await session.read_page("unknown"))
        self.assertFalse(disabled["page_reader_enabled"])
        self.assertEqual(disabled["page_read_attempts"], 0)
        session.reader = forbidden
        source = Source("known", "https://example.org/", "snippet")
        session.sources[source.url] = source
        for sid, focus, error in (
            ("unknown", "", "unknown_source_id"),
            (source.id, "x" * 121, "focus_too_long"),
            (source.id, "", "page_budget"),
        ):
            blocked = payload(await session.read_page(sid, focus))
            self.assertEqual(blocked["error"], error)
            self.assertEqual(blocked["page_read_attempts"], 0)

    async def test_concurrent_search_and_read_snapshots_respect_hard_budgets(self):
        search_calls, page_calls = [], []

        async def search(query, limit):
            search_calls.append(query)
            await asyncio.sleep(0)
            return [Source(query, "https://example.org/" + query, "snippet")]

        async def read(url, limit, **kwargs):
            page_calls.append(url)
            await asyncio.sleep(0)
            return "page"

        session = ResearchSession(ResearchConfig(max_searches=2, max_page_reads=1), search, read)
        responses = await asyncio.gather(*(session.search_web(str(i)) for i in range(5)))
        self.assertEqual(len(search_calls), 2)
        self.assertEqual([payload(r)["search_attempts"] for r in responses], [1, 2, 2, 2, 2])
        pages = await asyncio.gather(*(session.read_page(s.id) for s in session.sources.values()))
        self.assertEqual(len(page_calls), 1)
        for response in pages:
            self.assertEqual(payload(response)["page_read_attempts"], 1)
            self.assertEqual(payload(response)["remaining_page_reads"], 0)
            self.assertEqual(payload(response)["pages_read"], 1)

    async def test_cancellation_keeps_consumed_attempt_without_saving_page(self):
        started = asyncio.Event()

        async def read(url, limit, **kwargs):
            started.set()
            await asyncio.Event().wait()

        session = ResearchSession(ResearchConfig(), None, read)
        source = Source("known", "https://example.org/", "snippet")
        session.sources[source.url] = source
        task = asyncio.create_task(session.read_page(source.id))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        after = payload(await session.read_page(source.id))
        self.assertEqual(
            (after["page_read_attempts"], after["pages_read"], after["remaining_page_reads"]),
            (1, 0, 1),
        )
        self.assertEqual(after["error"], "already_attempted")
