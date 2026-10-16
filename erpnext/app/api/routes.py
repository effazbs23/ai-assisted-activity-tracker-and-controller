from __future__ import annotations

import datetime as dt

import requests
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from app.erp.client import ERPError
from app.security import Caller
from app.services import activities, executor, parties, proposals, sales
from app.services.errors import NotFound, ValidationFailed

router = APIRouter(prefix="/v1")


def _limit(request: Request, limit: int) -> int:
    return min(limit, request.app.state.settings.max_page_size)


def _erp(request: Request, caller: str):
    return request.app.state.erp_for(caller)


def _proposed(action):
    return {"action_id": action.id, "status": action.status, "preview": action.payload}


def _guard(fn, *a, **kw):
    try:
        return _proposed(fn(*a, **kw))
    except NotFound as exc:
        raise HTTPException(404, str(exc))
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


def _read(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except NotFound as exc:
        raise HTTPException(404, str(exc))
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc))


# -- reads ---------------------------------------------------------------------------------------------
@router.get("/clients")
def clients(request: Request, q: str = Query(min_length=2, max_length=100), limit: int = 10, caller: str = Caller):
    return parties.search_clients(_erp(request, caller), q, _limit(request, limit))


@router.get("/opportunities")
def opportunities(request: Request, q: str | None = Query(default=None, max_length=100), status: str | None = None,
                  limit: int = 10, caller: str = Caller):
    return parties.search_opportunities(_erp(request, caller), q, _limit(request, limit), status)


@router.get("/items")
def items(request: Request, q: str = Query(min_length=2, max_length=100), limit: int = 10, caller: str = Caller):
    return sales.search_items(_erp(request, caller), q, _limit(request, limit))


@router.get("/vocabulary")
def vocabulary(request: Request, limit: int = 40, caller: str = Caller):
    return parties.vocabulary(_erp(request, caller), _limit(request, limit))


@router.get("/activities")
def list_activities(request: Request, client: str | None = None, activity_type: str | None = None,
                    status: str | None = None, date_from: dt.date | None = None, date_to: dt.date | None = None,
                    mine: bool = False, limit: int = 20, caller: str = Caller):
    return _read(activities.list_activities, _erp(request, caller), _limit(request, limit), client, activity_type, status,
                 date_from, date_to, mine)


@router.get("/quotations")
def quotations(request: Request, customer: str | None = None, status: str | None = None, date_from: dt.date | None = None,
               date_to: dt.date | None = None, limit: int = 20, caller: str = Caller):
    return _read(sales.list_documents, _erp(request, caller), "Quotation", _limit(request, limit), customer, status, date_from, date_to)


@router.get("/sales-orders")
def sales_orders(request: Request, customer: str | None = None, status: str | None = None, date_from: dt.date | None = None,
                 date_to: dt.date | None = None, finalized: bool = False, limit: int = 20, caller: str = Caller):
    return _read(sales.list_documents, _erp(request, caller), "Sales Order", _limit(request, limit), customer, status,
                 date_from, date_to, finalized)


@router.get("/quotations/{doc_id}")
def quotation(doc_id: str, request: Request, caller: str = Caller):
    return _read(sales.get_document, _erp(request, caller), "Quotation", doc_id)


@router.get("/sales-orders/{doc_id}")
def sales_order(doc_id: str, request: Request, caller: str = Caller):
    return _read(sales.get_document, _erp(request, caller), "Sales Order", doc_id)


@router.get("/summary")
def summary(request: Request, doctype: str, customer: str | None = None, date_from: dt.date | None = None,
            date_to: dt.date | None = None, caller: str = Caller):
    return _read(sales.summary, _erp(request, caller), doctype, date_from, date_to, customer)


# -- proposals (nothing is written until the action is confirmed) -------------------------------------
class ActivityRequest(BaseModel):
    activity_type: str
    details: str = Field(default="", max_length=2000)
    status: str | None = None
    date: dt.date | None = None
    party_type: str | None = None
    party_id: str | None = None
    new_client_name: str | None = Field(default=None, max_length=140)


class NewItem(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    price: float = Field(gt=0, le=1_000_000)
    description: str = Field(default="", max_length=1000)
    uom: str | None = Field(default=None, max_length=40)
    item_group: str | None = Field(default=None, max_length=140)
    code: str | None = Field(default=None, max_length=140)
    allow_similar: bool = False


class Line(BaseModel):
    """Either an existing item_id (priced by ERPNext) or a new_item to create (priced by the caller)."""
    item_id: str | None = Field(default=None, min_length=1, max_length=140)
    new_item: NewItem | None = None
    quantity: float = Field(gt=0)

    @model_validator(mode="after")
    def one_of(self):
        if bool(self.item_id) == bool(self.new_item):
            raise ValueError("Give exactly one of item_id or new_item")
        return self


class QuotationRequest(BaseModel):
    party_type: str = "Customer"
    party_id: str
    lines: list[Line] = Field(min_length=1, max_length=50)
    valid_till: dt.date | None = None


class OrderRequest(BaseModel):
    customer_id: str
    lines: list[Line] = Field(min_length=1, max_length=50)
    delivery_date: dt.date | None = None


class OpportunityRequest(BaseModel):
    party_type: str = "Lead"
    party_id: str
    amount: float | None = Field(default=None, ge=0)
    sales_stage: str | None = None
    expected_closing: dt.date | None = None
    notes: str = Field(default="", max_length=1000)


class OpportunityUpdate(BaseModel):
    sales_stage: str | None = None
    status: str | None = None
    amount: float | None = Field(default=None, ge=0)


def _iso(d: dt.date | None):
    return d.isoformat() if d else None


@router.post("/items", status_code=202)
def propose_item(body: NewItem, request: Request, caller: str = Caller, idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.item, request.app.state, caller, body.name, body.price, body.description, body.uom,
                  body.item_group, body.code, body.allow_similar, idempotency_key)


@router.post("/activities", status_code=202)
def propose_activity(body: ActivityRequest, request: Request, caller: str = Caller,
                     idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.log_activity, request.app.state, caller, body.activity_type, body.details, body.status,
                  _iso(body.date), body.party_type, body.party_id, body.new_client_name, idempotency_key)


@router.post("/quotations", status_code=202)
def propose_quotation(body: QuotationRequest, request: Request, caller: str = Caller,
                      idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.quotation, request.app.state, caller, body.party_type, body.party_id,
                  [l.model_dump(exclude_none=True) for l in body.lines], _iso(body.valid_till), idempotency_key)


@router.post("/sales-orders", status_code=202)
def propose_order(body: OrderRequest, request: Request, caller: str = Caller,
                  idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.order, request.app.state, caller, body.customer_id, [l.model_dump(exclude_none=True) for l in body.lines],
                  _iso(body.delivery_date), idempotency_key)


@router.post("/quotations/{doc_id}/submit", status_code=202)
def propose_submit_quotation(doc_id: str, request: Request, caller: str = Caller):
    return _guard(proposals.submit_document, request.app.state, caller, "Quotation", doc_id)


@router.post("/sales-orders/{doc_id}/submit", status_code=202)
def propose_submit_order(doc_id: str, request: Request, caller: str = Caller):
    return _guard(proposals.submit_document, request.app.state, caller, "Sales Order", doc_id)


@router.post("/quotations/{doc_id}/to-order", status_code=202)
def propose_to_order(doc_id: str, request: Request, delivery_date: dt.date | None = None, caller: str = Caller):
    return _guard(proposals.quotation_to_order, request.app.state, caller, doc_id, _iso(delivery_date))


@router.post("/opportunities", status_code=202)
def propose_opportunity(body: OpportunityRequest, request: Request, caller: str = Caller,
                        idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.create_opportunity, request.app.state, caller, body.party_type, body.party_id, body.amount,
                  body.sales_stage, _iso(body.expected_closing), body.notes, idempotency_key)


@router.patch("/opportunities/{doc_id}", status_code=202)
def propose_opportunity_update(doc_id: str, body: OpportunityUpdate, request: Request, caller: str = Caller):
    return _guard(proposals.update_opportunity, request.app.state, caller, doc_id, body.sales_stage, body.status, body.amount)


# -- confirmation --------------------------------------------------------------------------------------
@router.post("/actions/{action_id}/confirm")
def confirm_action(action_id: str, request: Request, caller: str = Caller):
    st = request.app.state
    action = st.actions.get(action_id, caller)
    if not action:
        raise HTTPException(404, "Action not found")
    if action.status == "done":
        return {"action_id": action.id, "status": "done", "result": action.result}
    if action.status != "pending":
        raise HTTPException(410, f"Action is {action.status}")
    try:
        result = executor.execute(st, action)
    except NotFound as exc:
        raise HTTPException(404, str(exc))
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc))
    except ERPError as exc:
        st.audit.write(caller, "erp_error", action=action.id, error=str(exc))
        raise HTTPException(502, f"ERPNext rejected or could not process the request: {exc}")
    action.status, action.result = "done", result
    st.audit.write(caller, "execute", action=action.id, kind=action.kind, result=result)
    return {"action_id": action.id, "status": "done", "result": result}


@router.post("/actions/{action_id}/cancel")
def cancel_action(action_id: str, request: Request, caller: str = Caller):
    st = request.app.state
    action = st.actions.get(action_id, caller)
    if not action:
        raise HTTPException(404, "Action not found")
    if action.status == "pending":
        action.status = "cancelled"
        st.audit.write(caller, "cancel", action=action.id)
    return {"action_id": action.id, "status": action.status}


class PromptRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", min_length=1, max_length=100)


@router.post("/prompt")
def prompt(body: PromptRequest, request: Request, caller: str = Caller):
    try:
        out = request.app.state.agent.handle(caller, body.session_id, body.message)
    except (RuntimeError, requests.RequestException) as exc:
        raise HTTPException(503, f"AI service unavailable: {exc}")
    return {"session_id": body.session_id, **out}
