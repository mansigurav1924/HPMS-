try:
    import razorpay
except ImportError:
    razorpay = None
import hmac
import hashlib
from app.config import settings

class RazorpayService:
    def __init__(self):
        self.key_id = settings.RAZORPAY_KEY_ID
        self.key_secret = settings.RAZORPAY_KEY_SECRET
        self.webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        if self.key_id and self.key_secret and razorpay:
            self.client = razorpay.Client(auth=(self.key_id, self.key_secret))
        else:
            self.client = None

    def _to_paise(self, amount_inr: float) -> int:
        return int(round(float(amount_inr) * 100))
        
    def _to_inr(self, amount_paise: int) -> float:
        return float(amount_paise) / 100.0

    def create_order(self, amount: float, receipt: str, notes: dict = None) -> dict:
        if not self.client:
            return {"id": f"test_order_{receipt}", "amount": self._to_paise(amount), "currency": "INR"}
        
        data = {
            "amount": self._to_paise(amount),
            "currency": "INR",
            "receipt": receipt,
            "notes": notes or {}
        }
        return self.client.order.create(data=data)

    def fetch_order_payments(self, order_id: str) -> list:
        if not self.client:
            return []
        return self.client.order.payments(order_id).get('items', [])
        
    def fetch_payment(self, payment_id: str) -> dict:
        if not self.client:
            return {"id": payment_id, "status": "captured"}
        return self.client.payment.fetch(payment_id)
        
    def create_refund(self, payment_id: str, amount: float = None, notes: dict = None) -> dict:
        if not self.client:
            return {"id": "test_refund_xyz", "status": "processed"}
        data = {}
        if amount:
            data['amount'] = self._to_paise(amount)
        if notes:
            data['notes'] = notes
        return self.client.payment.refund(payment_id, data)
        
    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        if not self.webhook_secret:
            return True
        expected_sig = hmac.new(
            bytes(self.webhook_secret, 'utf-8'),
            msg=body,
            digestmod=hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_sig, signature)

razorpay_service = RazorpayService()
