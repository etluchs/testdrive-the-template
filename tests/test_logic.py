import appkit
import pytest

from app import logic


def test_list_requests_reads_sharepoint():
    rows = logic.list_requests()
    assert len(rows) == 4
    assert rows[0]["Title"] == "New laptop for onboarding"


def test_summarize_counts_by_status():
    summary = logic.summarize(logic.list_requests())
    assert summary.total == 4
    assert summary.by_status["Open"] == 2
    assert "Requests summary" in summary.text
    assert "New laptop for onboarding" in summary.text


def test_send_summary_emails_the_summary():
    summary = logic.send_summary("colleague@uzh.ch")
    sent = appkit.mail.outbox()
    assert len(sent) == 1
    assert sent[0]["to"] == ["colleague@uzh.ch"]
    assert str(summary.total) in sent[0]["subject"]


def test_send_summary_validates_recipient():
    with pytest.raises(ValueError):
        logic.send_summary("nope")
    assert appkit.mail.outbox() == []
