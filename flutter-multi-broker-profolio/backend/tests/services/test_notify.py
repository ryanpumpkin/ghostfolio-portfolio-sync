"""Failure alerts (spec §9, §10).

An alert exists to interrupt, and earns that right only by being rare.
These tests pin the two properties that keep it rare and useful: it
fires only on failure, and the subject alone carries the message.
"""

from __future__ import annotations

from app.services.notify import MAX_BODY_CHARS, compose_failure


class TestSubject:
    def test_subject_says_what_and_how_many(self) -> None:
        """A lock screen shows the subject and nothing else. 'sync
        failed' makes you open a laptop to learn anything."""
        subject, _ = compose_failure(
            job="sync-all", failures=2, detail="", host="main"
        )
        assert "sync-all" in subject
        assert "2 source(s) failed" in subject
        assert "main" in subject

    def test_host_is_optional(self) -> None:
        subject, _ = compose_failure(job="sync-all", failures=1, detail="")
        assert subject.endswith("1 source(s) failed")


class TestBody:
    def test_it_says_what_was_and_was_not_damaged(self) -> None:
        """The first question on reading this at 7am is 'is my data
        broken'. The answer is no, and it should not require reasoning."""
        _, body = compose_failure(job="sync-all", failures=1, detail="boom")
        assert "stale" in body
        assert "Nothing was" in body and "corrupted" in body
        assert "boom" in body

    def test_long_output_keeps_the_tail(self) -> None:
        """Errors and the summary land at the END of a job log; the head
        is the same startup chatter every run produces."""
        detail = "chatter\n" * 5000 + "THE ACTUAL ERROR"
        _, body = compose_failure(job="sync-all", failures=1, detail=detail)
        assert "THE ACTUAL ERROR" in body
        assert "earlier output trimmed" in body
        assert len(body) < MAX_BODY_CHARS + 1000

    def test_short_output_is_not_trimmed(self) -> None:
        _, body = compose_failure(job="sync-all", failures=1, detail="brief")
        assert "trimmed" not in body


class TestOnlyOnFailure:
    def test_zero_failures_sends_nothing(self, monkeypatch) -> None:
        """A daily 'sync ok' mail is read for a week, filtered for a
        month, then invisible — taking the one that mattered with it."""
        import io
        import sys

        from tools.notify.__main__ import main

        sent: list = []
        monkeypatch.setattr(
            "tools.notify.__main__.send_alert",
            lambda *a, **k: sent.append(a),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("all good"))
        assert main(["--job", "sync-all", "--failures", "0"]) == 0
        assert sent == []

    def test_a_send_failure_does_not_change_the_exit_code(
        self, monkeypatch
    ) -> None:
        """A sync failure must not become a mail failure — the real
        cause has to survive."""
        import io
        import sys

        from tools.notify.__main__ import main

        def boom(*a, **k):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("tools.notify.__main__.send_alert", boom)
        monkeypatch.setenv("MBP_GMAIL_DIGEST_RECIPIENT", "a@b.c")
        monkeypatch.setenv("MBP_GMAIL_FROM_EMAIL", "a@b.c")
        monkeypatch.setenv("MBP_GMAIL_APP_PASSWORD", "x")
        monkeypatch.setattr(sys, "stdin", io.StringIO("boom"))
        assert main(["--job", "sync-all", "--failures", "1"]) == 0

    def test_missing_credentials_is_reported_not_raised(
        self, monkeypatch
    ) -> None:
        import io
        import sys

        from tools.notify.__main__ import main

        for var in ("MBP_GMAIL_DIGEST_RECIPIENT", "MBP_GMAIL_FROM_EMAIL",
                    "MBP_GMAIL_APP_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("boom"))
        assert main(["--job", "sync-all", "--failures", "1"]) == 0
