import datetime as dt

from tests.test_api import H


def tomorrow_at(h, m=0):
    d = dt.date.today() + dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(h, m)).isoformat()


def book(client, start, **kw):
    return client.post("/v1/calendar/events", headers=H, json={"name": "Demo", "start": start, "duration_min": 30, **kw})


def test_book_event_and_conflict(ctx):
    client, odoo, _ = ctx
    r = book(client, tomorrow_at(11))
    assert r.status_code == 202
    assert client.post(f"/v1/actions/{r.json()['action_id']}/confirm", headers=H).status_code == 200
    assert len(odoo.t["calendar.event"]) == 1
    # overlapping booking is refused at proposal time
    assert book(client, tomorrow_at(11, 15)).status_code == 422
    assert book(client, tomorrow_at(11, 30)).status_code == 202  # back-to-back is fine


def test_slot_taken_between_proposal_and_confirm(ctx):
    client, odoo, _ = ctx
    a = book(client, tomorrow_at(14)).json()["action_id"]
    b = book(client, tomorrow_at(14), name="Other").json()["action_id"]
    assert client.post(f"/v1/actions/{a}/confirm", headers=H).status_code == 200
    assert client.post(f"/v1/actions/{b}/confirm", headers=H).status_code == 422
    assert len(odoo.t["calendar.event"]) == 1


def test_rejects_past_and_unknown_attendee(ctx):
    client, *_ = ctx
    assert book(client, "2020-01-01T10:00").status_code == 422
    assert book(client, tomorrow_at(10), attendee_ids=[9999]).status_code == 404


def test_availability_excludes_busy_slot(ctx):
    client, *_ = ctx
    aid = book(client, tomorrow_at(10)).json()["action_id"]
    client.post(f"/v1/actions/{aid}/confirm", headers=H)
    day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    r = client.get(f"/v1/calendar/availability?date={day}&duration_min=60", headers=H).json()
    starts = [s["start"][11:16] for s in r["free_slots"]]
    assert "09:00" in starts and "10:00" not in starts and "10:30" in starts
    assert len(r["busy"]) == 1
