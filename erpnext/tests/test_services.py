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


# -- creating items ----------------------------------------------------------------------------------
def test_preview_item_generates_a_unique_code_and_defaults(erp):
    p = sales.preview_item(erp, {"name": "Glass Whiteboard XL", "price": 300})
    assert p == {"code": "GLASS-WHITEBOARD-XL", "name": "Glass Whiteboard XL", "price": 300.0, "description": "",
                 "uom": "Nos", "item_group": "Products"}
    erp.db["Item"].append({"name": "GLASS-WHITEBOARD-XL", "item_name": "Something else entirely", "disabled": 0, "is_sales_item": 1})
    assert sales.preview_item(erp, {"name": "Glass Whiteboard XL", "price": 300})["code"] == "GLASS-WHITEBOARD-XL-2"


@pytest.mark.parametrize("spec", [{"name": "x", "price": 10}, {"name": "Fine Name"}, {"name": "Fine Name", "price": 0},
                                   {"name": "Fine Name", "price": -5}, {"name": "Fine Name", "price": True},
                                   {"name": "Fine Name", "price": 5_000_000}, {"name": "N" * 141, "price": 5}])
def test_preview_item_rejects_bad_input(erp, spec):
    with pytest.raises(ValidationFailed):
        sales.preview_item(erp, spec)


def test_preview_item_blocks_duplicates_and_asks_about_lookalikes(erp):
    with pytest.raises(ValidationFailed, match="already exists"):
        sales.preview_item(erp, {"name": "ergonomic chair", "price": 99})            # exact, any case
    with pytest.raises(ValidationFailed, match="very similar"):
        sales.preview_item(erp, {"name": "Ergonomic Chairs", "price": 99})
    assert sales.preview_item(erp, {"name": "Ergonomic Chairs", "price": 99, "allow_similar": True})["code"] == "ERGONOMIC-CHAIRS"
    assert sales.preview_item(erp, {"name": "Ergonomic Chairs", "price": 99}, ignore_ids={"CHAIR-ERG"})["name"] == "Ergonomic Chairs"


def test_create_item_writes_item_and_selling_price(erp):
    p = sales.preview_item(erp, {"name": "Glass Whiteboard", "price": 300, "description": "Wall mounted"})
    out = sales.create_item(erp, p)
    assert out["id"] == "GLASS-WHITEBOARD" and out["list_price"] == 300
    item = next(d for t, d in erp.created if t == "Item")
    assert item["is_stock_item"] == 0 and item["is_sales_item"] == 1 and item["standard_rate"] == 300 and item["stock_uom"] == "Nos"
    assert [t for t, _ in erp.created] == ["Item"]          # ERPNext made the Standard Selling price itself
    assert sales.item_rates(erp, ["GLASS-WHITEBOARD"]) == {"GLASS-WHITEBOARD": 300}


def test_item_price_is_added_only_when_erpnext_did_not_create_it(erp):
    erp.auto_item_price = False
    sales.create_item(erp, sales.preview_item(erp, {"name": "Glass Whiteboard", "price": 300}))
    price = next(d for t, d in erp.created if t == "Item Price")
    assert price["price_list"] == "Standard Selling" and price["price_list_rate"] == 300


def test_failed_price_entry_removes_the_new_item(erp):
    erp.auto_item_price = False
    erp.deny_insert = {"Item Price"}
    with pytest.raises(Exception, match="removed again"):
        sales.create_item(erp, sales.preview_item(erp, {"name": "Glass Whiteboard", "price": 300}))
    assert ("Item", "GLASS-WHITEBOARD") in erp.deleted and not [i for i in erp.db["Item"] if i["name"] == "GLASS-WHITEBOARD"]


def test_inline_new_item_line_in_a_preview(erp):
    p = sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders",
                            [{"item_id": "WS-4", "quantity": 2}, {"new_item": {"name": "Glass Whiteboard", "price": 300}, "quantity": 5}])
    assert [l["item_id"] for l in p["lines"]] == ["WS-4", "GLASS-WHITEBOARD"]
    assert p["lines"][1]["new_item"]["code"] == "GLASS-WHITEBOARD" and p["total_before_tax"] == 2 * 360 + 5 * 300
    assert erp.created == []   # a preview writes nothing


@pytest.mark.parametrize("line", [{"quantity": 1}, {"item_id": "WS-4", "new_item": {"name": "Glass Whiteboard", "price": 3}, "quantity": 1},
                                   {"new_item": {"name": "Glass Whiteboard"}, "quantity": 1}])
def test_inline_lines_need_exactly_one_item_source_and_a_price(erp, line):
    with pytest.raises(ValidationFailed):
        sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders", [line])


def test_two_new_lines_cannot_share_a_name(erp):
    spec = {"name": "Glass Whiteboard", "price": 300}
    with pytest.raises(ValidationFailed, match="twice"):
        sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders", [{"new_item": spec, "quantity": 1}, {"new_item": dict(spec), "quantity": 2}])
    a, b = {"name": "Glass Board A", "price": 1}, {"name": "Glass Board A1", "price": 1}   # different names, distinct codes
    p = sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders", [{"new_item": a, "quantity": 1}, {"new_item": b, "quantity": 1}])
    assert len({l["item_id"] for l in p["lines"]}) == 2


def test_item_that_cannot_be_deleted_is_disabled_so_it_can_never_be_sold(erp):
    erp.auto_item_price = False
    erp.deny_insert, erp.deny_delete = {"Item Price"}, {"Item"}
    with pytest.raises(Exception, match="could not be removed; it is disabled"):
        sales.create_item(erp, sales.preview_item(erp, {"name": "Glass Whiteboard", "price": 300}))
    assert next(i for i in erp.db["Item"] if i["name"] == "GLASS-WHITEBOARD")["disabled"] == 1


def test_inline_creation_creates_items_then_the_document(erp):
    p = sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders", [{"new_item": {"name": "Glass Whiteboard", "price": 300}, "quantity": 5}])
    out = sales.create_document(erp, p)
    assert out["created_items"] == ["GLASS-WHITEBOARD"] and out["doctype"] == "Quotation"
    assert [t for t, _ in erp.created] == ["Item", "Quotation"]
    quote = erp.created[-1][1]
    assert quote["items"][0] == {"item_code": "GLASS-WHITEBOARD", "qty": 5, "rate": 300}


def test_failed_document_rolls_the_new_items_back(erp):
    p = sales.build_preview(erp, "Quotation", "Customer", "XYZ Traders", [{"new_item": {"name": "Glass Whiteboard", "price": 300}, "quantity": 5}])
    erp.deny_insert = {"Quotation"}
    with pytest.raises(Exception, match="removed again"):
        sales.create_document(erp, p)
    assert ("Item", "GLASS-WHITEBOARD") in erp.deleted and any(t == "Item Price" for t, _ in erp.deleted)
    assert not [i for i in erp.db["Item"] if i["name"] == "GLASS-WHITEBOARD"]
