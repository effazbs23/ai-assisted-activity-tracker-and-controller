from __future__ import annotations

import datetime as dt

import requests
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from app.odoo.client import OdooError
from app.security import Caller
from app.services import calendar, crm, executor, proposals, sales
from app.services.errors import NotFound, ValidationFailed

router = APIRouter(prefix="/v1")


def _limit(request: Request, limit: int) -> int:
    return min(limit, request.app.state.settings.max_page_size)


class NewProduct(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    price: float = Field(gt=0, le=1_000_000)
    description: str = Field(default="", max_length=1000)
    default_code: str = Field(default="", max_length=40)
    allow_similar: bool = False


class OrderLine(BaseModel):
    """Either an existing product_id (priced by Odoo) or a new_product to create (priced by the caller)."""
    product_id: int | None = Field(default=None, gt=0)
    new_product: NewProduct | None = None
    quantity: float = Field(gt=0)

    @model_validator(mode="after")
    def one_of(self):
        if bool(self.product_id) == bool(self.new_product):
            raise ValueError("Give exactly one of product_id or new_product")
        return self


class OrderRequest(BaseModel):
    customer_id: int = Field(gt=0)
    lines: list[OrderLine] = Field(min_length=1, max_length=50)


class ActivityRequest(BaseModel):
    summary: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=4000)
    date: dt.date | None = None


@router.get("/customers")
def customers(request: Request, q: str = Query(min_length=2, max_length=100), limit: int = 10, caller: str = Caller):
    return crm.search_customers(request.app.state.odoo, q, _limit(request, limit))


@router.get("/vocabulary")
def vocabulary(request: Request, limit: int = 40, caller: str = Caller):
    return crm.vocabulary(request.app.state.odoo, _limit(request, limit))


@router.get("/products")
def products(request: Request, q: str = Query(min_length=2, max_length=100), limit: int = 10, caller: str = Caller):
    return sales.search_products(request.app.state.odoo, q, _limit(request, limit))


@router.get("/leads")
def leads(request: Request, q: str | None = Query(default=None, max_length=100), limit: int = 10, caller: str = Caller):
    return crm.search_leads(request.app.state.odoo, q, _limit(request, limit))


@router.get("/sales-orders")
def sales_orders(request: Request, customer: str | None = None, state: str | None = None,
                 date_from: dt.date | None = None, date_to: dt.date | None = None, limit: int = 20, caller: str = Caller):
    try:
        return sales.list_orders(request.app.state.odoo, _limit(request, limit), customer, state, date_from, date_to)
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc))


@router.get("/sales-orders/{order_id}")
def sales_order(order_id: int, request: Request, caller: str = Caller):
    try:
        return sales.get_order(request.app.state.odoo, order_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc))


@router.post("/products", status_code=202)
def propose_product(body: NewProduct, request: Request, caller: str = Caller,
                    idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.product, request.app.state, caller, body.name, body.price, body.description,
                  body.default_code, idempotency_key, body.allow_similar)


@router.get("/quotations")
def quotations(request: Request, customer: str | None = None, date_from: dt.date | None = None,
               date_to: dt.date | None = None, limit: int = 20, caller: str = Caller):
    return sales.list_quotations(request.app.state.odoo, _limit(request, limit), customer, date_from, date_to)


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


@router.post("/leads/{lead_id}/activities", status_code=202)
def propose_activity(lead_id: int, body: ActivityRequest, request: Request, caller: str = Caller,
                     idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.activity, request.app.state, caller, lead_id, body.summary, body.note,
                  body.date.isoformat() if body.date else None, idempotency_key)


@router.post("/sales-orders", status_code=202)
def propose_order(body: OrderRequest, request: Request, caller: str = Caller,
                  idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.order, request.app.state, caller, body.customer_id,
                  [l.model_dump(exclude_none=True) for l in body.lines], idempotency_key)


@router.post("/sales-orders/{order_id}/confirm", status_code=202)
def propose_confirm(order_id: int, request: Request, caller: str = Caller):
    return _guard(proposals.confirm_order, request.app.state, caller, order_id)


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
    except OdooError as exc:
        st.audit.write(caller, "odoo_error", action=action.id, error=str(exc))
        raise HTTPException(502, "Odoo rejected or could not process the request")
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


class EventRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    start: dt.datetime
    duration_min: int = Field(default=30, ge=5, le=480)
    attendee_ids: list[int] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=4000)


@router.get("/calendar/availability")
def availability(request: Request, date: dt.date, duration_min: int = 30, caller: str = Caller):
    st = request.app.state
    try:
        me = calendar.organizer_partner_id(st.odoo, st.odoo.uid or st.odoo.authenticate())
        return calendar.availability(st.odoo, st.settings, [me], date, duration_min)
    except ValidationFailed as exc:
        raise HTTPException(422, str(exc))


@router.post("/calendar/events", status_code=202)
def propose_event(body: EventRequest, request: Request, caller: str = Caller,
                  idempotency_key: str | None = Header(default=None)):
    return _guard(proposals.event, request.app.state, caller, body.name, body.start, body.duration_min,
                  body.attendee_ids, body.description, idempotency_key)


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
