import appkit


def test_index_lists_sharepoint_rows(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Access &amp; equipment requests" in body
    # Rows from the fake SharePoint list are rendered.
    assert "New laptop for onboarding" in body
    assert "Conference travel approval" in body


def test_index_uses_accessible_table_markup(client):
    body = client.get("/").text
    assert '<html lang="en">' in body
    assert 'class="skip-link"' in body
    assert '<th scope="col">' in body
    assert '<th scope="row">' in body   # row_header column
    assert '<caption' in body


def test_index_nav_marks_current_page(client):
    body = client.get("/").text
    assert 'aria-current="page"' in body


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_mail_summary_sends_and_confirms(client):
    resp = client.post("/mail-summary", data={"recipient": "boss@uzh.ch"})
    assert resp.status_code == 200
    assert "Summary sent" in resp.text
    sent = appkit.mail.outbox()
    assert len(sent) == 1
    assert sent[0]["to"] == ["boss@uzh.ch"]
    assert "Requests summary" in sent[0]["subject"]


def test_mail_summary_rejects_bad_address(client):
    resp = client.post("/mail-summary", data={"recipient": "not-an-email"})
    assert resp.status_code == 400
    assert "valid email" in resp.text
    assert appkit.mail.outbox() == []


def test_form_field_has_associated_label(client):
    body = client.get("/").text
    assert '<label class="field__label" for="recipient">' in body
    assert 'id="recipient"' in body
    assert 'name="recipient"' in body
