import csv
from pathlib import Path

import pytest

from app.agent.orchestrator import is_non_sales

SALES_OR_MIXED = [
    "Show my open opportunities", "What quotations did we send last month?", "Summarise my sales this month",
    "Met XYZ Interiors today and sent a quotation awaiting BOQ", "Create a quotation for Nizwa Builders for 5 workstations",
    "Find customer Nizwa Builders", "Find item workstation", "Log a meeting with the project team",
    "Log that I called the supplier", "Create a note for customer Nizwa Builders", "Hello", "What time is it in Muscat?",
]
NON_SALES = [
    "list all warehouses", "How many projects do we have?", "list all users", "show the stock balance report",
    "Create a note titled MCP test note with the content hello", "List all customer groups", "List all item groups",
    "list unpaid sales invoices", "How many employees do we have?", "Add a todo to call Nizwa Builders tomorrow",
    "Show leave applications", "List all price lists", "How many suppliers do we have?",
]


@pytest.mark.parametrize("text", SALES_OR_MIXED)
def test_sales_and_unclear_requests_keep_the_two_phase_path(text):
    assert not is_non_sales(text)


@pytest.mark.parametrize("text", NON_SALES)
def test_clearly_non_sales_requests_go_straight_to_mcp(text):
    assert is_non_sales(text)


def test_built_in_sales_prompts_in_the_csv_never_skip_phase_1():
    rows = csv.DictReader(open(Path(__file__).parent.parent / "docs" / "prompts.csv"))
    sales = [r["prompt"] for r in rows if r["category"].startswith("Sales (built-in)")]
    assert sales and not any(is_non_sales(p) for p in sales)
