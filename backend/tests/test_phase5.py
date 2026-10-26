import pytest
pytestmark = pytest.mark.skip(reason="payment feature paused")
from fastapi.testclient import TestClient
import json
import uuid
import hmac
import hashlib
from app.main import app
from app.config import settings
from unittest.mock import patch, MagicMock

client = TestClient(app)

@pytest.fixture
def mock_razorpay():
    with patch("app.services.razorpay_service.razorpay_service.verify_webhook_signature", return_value=True) as m1, \
         patch("app.routers.payments.generate_receipt_pdf.delay") as m2:
        yield m1, m2

def test_invalid_signature():
    with patch("app.services.razorpay_service.razorpay_service.verify_webhook_signature", return_value=False):
        response = client.post("/webhooks/payment", json={"event": "test"}, headers={"x-razorpay-signature": "invalid"})
        assert response.status_code == 400
        assert "Invalid signature" in response.json()["detail"]

@patch("app.routers.payments.psycopg.connect")
def test_webhook_replay(mock_connect, mock_razorpay):
    # Mock DB connection
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    payload = {
        "account_id": "acc_123",
        "event": "payment.captured",
        "created_at": 123456,
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "order_id": "order_xyz",
                    "amount": 10000,
                    "currency": "INR"
                }
            }
        }
    }
    
    # 1st time success
    mock_cursor.fetchone.return_value = {"payment_id": str(uuid.uuid4()), "amount": 100.00} # 100 INR = 10000 paise
    res1 = client.post("/webhooks/payment", json=payload, headers={"x-razorpay-signature": "valid", "x-razorpay-event-id": "ev_123"})
    assert res1.status_code == 200
    assert res1.json()["status"] == "ok"
    
    # Simulate replay (UniqueViolation)
    import psycopg
    mock_cursor.execute.side_effect = psycopg.errors.UniqueViolation("already processed")
    res2 = client.post("/webhooks/payment", json=payload, headers={"x-razorpay-signature": "valid", "x-razorpay-event-id": "ev_123"})
    assert res2.status_code == 200
    assert res2.json()["status"] == "already processed"

@patch("app.routers.bookings.get_db_connection")
@patch("app.routers.bookings.require_role")
def test_pay_booking_computes_amount(mock_require_role, mock_get_db, mock_razorpay):
    # Client-sent amount is ignored, server computes it.
    mock_require_role.return_value = str(uuid.uuid4())
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    # Mock fetching booking details
    mock_cursor.fetchone.side_effect = [
        {"status": "payment_pending", "rent_amount": 5000, "deposit_amount": 5000, "prop_name": "Test"}, # booking
        None, # no pending payment
        {"payment_id": str(uuid.uuid4())} # inserted payment
    ]
    
    # The client can't even send an amount in the body because it's not in the request model
    # So we just post to the url
    res = client.post(f"/bookings/{uuid.uuid4()}/pay", headers={"Authorization": "Bearer test"})
    
    assert res.status_code == 200
    assert "order_id" in res.json()
    assert res.json()["amount"] == 1000000 # 10000 INR in paise

@patch("app.routers.payments.psycopg.connect")
def test_amount_mismatch_flagged(mock_connect, mock_razorpay):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    mock_cursor.fetchone.return_value = {"payment_id": str(uuid.uuid4()), "amount": 5000.00} # DB has 5000
    
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "order_id": "order_xyz",
                    "amount": 200000, # Gateway has 2000 INR
                    "currency": "INR"
                }
            }
        }
    }
    
    res = client.post("/webhooks/payment", json=payload, headers={"x-razorpay-signature": "valid", "x-razorpay-event-id": "ev_abc"})
    assert res.status_code == 200
    assert res.json()["status"] == "flagged"

@patch("app.routers.bookings.get_db_connection")
@patch("app.routers.bookings.require_role")
def test_pay_booking_idempotent(mock_require_role, mock_get_db, mock_razorpay):
    mock_require_role.return_value = str(uuid.uuid4())
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    mock_cursor.fetchone.side_effect = [
        {"status": "payment_pending", "rent_amount": 5000, "deposit_amount": 5000, "prop_name": "Test"}, # booking
        {"gateway_order_id": "existing_order_abc", "amount": 10000} # pending payment
    ]
    
    res = client.post(f"/bookings/{uuid.uuid4()}/pay", headers={"Authorization": "Bearer test"})
    assert res.status_code == 200
    assert res.json()["order_id"] == "existing_order_abc"
