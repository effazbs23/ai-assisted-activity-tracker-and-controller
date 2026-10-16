import datetime as dt

import pytest

from app.services import activities, parties, sales
from app.services.errors import NotFound, ValidationFailed
from app.services.search import name_filters, similarity, word_stems
from tests.stub_erp import StubERPNext


@pytest.fixture
def erp():
    return StubERPNext()


def test_word_stems_and_filters():
    assert word_stems("20 Workstations") == ["20", "workstation"]
    assert name_filters("item_name", "ergonomic chairs") == [["item_name", "like", "%ergonomic%"], ["item_name", "like", "%chair%"]]


def test_similarity_tolerates_speech_slips():
    assert similarity("Nazwa Builders", "Nizwa Builders") > 0.8
    assert similarity("Gulf Interior", "Gulf Interiors LLC") > 0.8
    assert similarity("Zanzibar Spices", "Nizwa Builders") < 0.5


def test_search_clients_exact_beats_fuzzy_and_tags_types(erp):
    rows = parties.search_clients(erp, "xyz", 10)
    assert {(r["type"], r["name"]) for r in rows} == {("Customer", "XYZ Traders"), ("Lead", "XYZ Interiors")}
    assert all("match" not in r for r in rows)


def test_search_clients_fuzzy_fallback(erp):
    rows = parties.search_clients(erp, "Nazwa Builders", 10)
    assert rows[0]["name"] == "Nizwa Builders" and rows[0]["match"] == "fuzzy"
    assert parties.search_clients(erp, "Zanzibar Spices Intl", 10) == []


def test_party_name_lookup(erp):
    assert parties.party_name(erp, "Lead", "CRM-LEAD-0001") == "XYZ Interiors"
    assert parties.party_name(erp, "Opportunity", "CRM-OPP-0001") == "Gulf Interiors LLC"
    with pytest.raises(NotFound):
        parties.party_name(erp, "Customer", "Nobody")
    with pytest.raises(NotFound):
        parties.party_name(erp, "Supplier", "x")


def test_item_price_comes_from_price_list_then_standard_rate(erp):
    assert sales.item_rates(erp, ["WS-4", "CHAIR-ERG"]) == {"WS-4": 360, "CHAIR-ERG": 95}  # price list wins, standard rate fills in
    erp.deny = {"Item Price"}  # a role that cannot read Item Price still gets the standard rate
    assert sales.item_rates(erp, ["WS-4"]) == {"WS-4": 350}


def test_search_items_hides_disabled_and_fuzzy_matches(erp):
    assert [i["id"] for i in sales.search_items(erp, "retired", 5)] != ["OLD-1"]
    assert sales.search_items(erp, "Ergonomic Chairs", 5)[0]["list_price"] == 95
    near = sales.search_items(erp, "Ergonomik Chair", 5)
    assert near[0]["id"] == "CHAIR-ERG" and near[0]["match"] == "fuzzy"


def test_build_preview_merges_duplicate_lines_and_totals(erp):
    p = sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders",
                            [{"item_id": "WS-4", "quantity": 2}, {"item_id": "WS-4", "quantity": 3}, {"item_id": "CHAIR-ERG", "quantity": 1}])
    assert p["lines"][0]["quantity"] == 5 and p["total_before_tax"] == 5 * 360 + 95 and p["currency"] == "OMR"
    assert p["valid_till"] > dt.date.today().isoformat()


@pytest.mark.parametrize("lines", [[], [{"item_id": "WS-4"}], [{"item_id": "WS-4", "quantity": -1}],
                                    [{"item_id": "WS-4", "quantity": 10_001}], [{"item_id": "WS-4", "quantity": True}],
                                    [{"quantity": 2}]])
def test_build_preview_rejects_bad_lines(erp, lines):
    with pytest.raises(ValidationFailed):
        sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders", lines)


def test_order_preview_defaults_and_rejects_past_dates(erp):
    p = sales.build_preview(erp, "Sales Order", "Customer", "XYZ Traders", [{"item_id": "WS-4", "quantity": 1}])
    assert p["delivery_date"] == (dt.date.today() + dt.timedelta(days=14)).isoformat()
    with pytest.raises(ValidationFailed):
        sales.build_preview(erp, "Sales Order", "Customer", "XYZ Traders", [{"item_id": "WS-4", "quantity": 1}], delivery_date="2020-01-01")
    with pytest.raises(ValidationFailed):
        sales.build_preview(erp, "Sales Order", "Customer", "XYZ Traders", [{"item_id": "WS-4", "quantity": 1}], delivery_date="soon")


def test_list_documents_filters(erp):
    today = dt.date.today()
    assert [q["name"] for q in sales.list_documents(erp, "Quotation", 10, customer="gulf")] == ["SAL-QTN-0003"]
    assert [q["name"] for q in sales.list_documents(erp, "Quotation", 10, status="Draft")] == ["SAL-QTN-0002"]
    month = sales.list_documents(erp, "Quotation", 10, date_from=today - dt.timedelta(days=60), date_to=today - dt.timedelta(days=30))
    assert [q["name"] for q in month] == ["SAL-QTN-0001"]
    assert sales.list_documents(erp, "Sales Order", 10, finalized=True)[0]["docstatus"] == 1


def test_summary_counts_and_totals_by_status(erp):
    s = sales.summary(erp, "Quotation", None, None)
    assert s == {"doctype": "Quotation", "by_status": [{"status": "Open", "count": 2, "total": 1250.0},
                                                       {"status": "Draft", "count": 1, "total": 500.0}],
                 "count": 3, "total": 1750.0, "currency": "OMR", "truncated": False}
    assert sales.summary(erp, "Opportunity", None, None)["total"] == 8500


def test_activity_normalisation():
    assert activities.normalize_type("quotation") == "Quotation Sent"
    assert activities.normalize_type("BOQ") == "BOQ Received"
    assert activities.normalize_type("meeting") == "Meeting"
    assert activities.normalize_status(None) == "Open"
    assert activities.normalize_status("waiting") == "Awaiting Client"
    with pytest.raises(ValidationFailed):
        activities.normalize_type("dancing")
    with pytest.raises(ValidationFailed):
        activities.normalize_status("whatever")


def test_activity_preview_needs_a_client_and_a_valid_date(erp):
    with pytest.raises(ValidationFailed):
        activities.preview(erp, "Meeting", "x", None, None, None, None, None)
    with pytest.raises(ValidationFailed):
        activities.preview(erp, "Meeting", "x", None, "13/45/2026", "Customer", "XYZ Traders", None)
    p = activities.preview(erp, "Call", "  spoke  ", "done", None, None, "XYZ Traders", None)
    assert p["party_type"] == "Customer" and p["status"] == "Completed" and p["details"] == "spoke"
    assert p["date"] == dt.date.today().isoformat()


def test_activity_listing_filters(erp):
    assert len(activities.list_activities(erp, 10)) == 2
    assert [a["name"] for a in activities.list_activities(erp, 10, activity_type="meeting")] == ["SACT-00001"]
    assert [a["name"] for a in activities.list_activities(erp, 10, mine=True)] == ["SACT-00001"]
    assert [a["name"] for a in activities.list_activities(erp, 10, status="awaiting")] == ["SACT-00002"]
