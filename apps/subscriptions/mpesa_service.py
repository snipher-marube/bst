"""
M-Pesa Daraja API service – Lipa Na M-Pesa Online (STK Push).

Sandbox base URL:  https://sandbox.safaricom.co.ke
Production URL:    https://api.safaricom.co.ke

The service reads credentials from Django settings so you can swap between
sandbox and production by flipping MPESA_SANDBOX in the environment.
"""
import base64
import logging
from datetime import datetime

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_SANDBOX_BASE = 'https://sandbox.safaricom.co.ke'
_PRODUCTION_BASE = 'https://api.safaricom.co.ke'


def _base_url() -> str:
    return _SANDBOX_BASE if getattr(settings, 'MPESA_SANDBOX', True) else _PRODUCTION_BASE


class MpesaService:
    """Thin wrapper around the Daraja STK Push (Lipa Na M-Pesa Online) flow."""

    # ------------------------------------------------------------------ #
    # OAuth token
    # ------------------------------------------------------------------ #

    def get_access_token(self) -> str:
        """Return a fresh Bearer token from Daraja OAuth endpoint."""
        consumer_key = settings.MPESA_CONSUMER_KEY
        consumer_secret = settings.MPESA_CONSUMER_SECRET

        if not consumer_key or not consumer_secret:
            raise ValueError(
                "MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET must be set in settings."
            )

        url = f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials"
        try:
            response = requests.get(url, auth=(consumer_key, consumer_secret), timeout=15)
            response.raise_for_status()
            token = response.json().get('access_token', '')
            if not token:
                raise ValueError("Empty access_token in Daraja OAuth response.")
            return token
        except requests.RequestException as exc:
            logger.error("M-Pesa OAuth request failed: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _timestamp() -> str:
        """Return current EAT timestamp as YYYYMMDDHHMMSS (required by Daraja)."""
        return datetime.now().strftime('%Y%m%d%H%M%S')

    @staticmethod
    def _password(shortcode: str, passkey: str, timestamp: str) -> str:
        """Base64-encode the STK push password: shortcode + passkey + timestamp."""
        raw = f"{shortcode}{passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    @staticmethod
    def _format_phone(phone: str) -> str:
        """
        Normalise a Kenyan phone number to the 2547XXXXXXXX format
        that Safaricom expects.
        """
        phone = phone.strip().replace(' ', '').replace('-', '')
        if phone.startswith('+254'):
            phone = '254' + phone[4:]
        elif phone.startswith('0'):
            phone = '254' + phone[1:]
        elif phone.startswith('7') or phone.startswith('1'):
            phone = '254' + phone
        return phone

    # ------------------------------------------------------------------ #
    # STK Push (initiate payment)
    # ------------------------------------------------------------------ #

    def lipa_na_mpesa_online(
        self,
        phone_number: str,
        amount: int,
        account_reference: str,
        transaction_desc: str,
    ) -> dict:
        """
        Send a Lipa Na M-Pesa Online STK Push request.

        Returns the raw Daraja JSON response dict.
        Raises on network/API errors.

        :param phone_number:      Kenyan number (any normalised format).
        :param amount:            Integer KES amount (Daraja rejects decimals).
        :param account_reference: Shown on the customer's phone (max 12 chars).
        :param transaction_desc:  Short description (max 13 chars).
        """
        token = self.get_access_token()
        shortcode = settings.MPESA_SHORTCODE
        passkey = settings.MPESA_PASSKEY
        callback_url = settings.MPESA_CALLBACK_URL

        timestamp = self._timestamp()
        password = self._password(shortcode, passkey, timestamp)
        phone = self._format_phone(phone_number)

        payload = {
            'BusinessShortCode': shortcode,
            'Password': password,
            'Timestamp': timestamp,
            'TransactionType': 'CustomerPayBillOnline',
            'Amount': int(amount),
            'PartyA': phone,
            'PartyB': shortcode,
            'PhoneNumber': phone,
            'CallBackURL': callback_url,
            'AccountReference': account_reference[:12],
            'TransactionDesc': transaction_desc[:13],
        }

        url = f"{_base_url()}/mpesa/stkpush/v1/processrequest"
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("STK Push request failed: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    # STK Push Query (check status)
    # ------------------------------------------------------------------ #

    def query_stk_push(self, checkout_request_id: str) -> dict:
        """
        Query the status of a previous STK Push request.
        Returns the raw Daraja JSON response.
        """
        token = self.get_access_token()
        shortcode = settings.MPESA_SHORTCODE
        passkey = settings.MPESA_PASSKEY
        timestamp = self._timestamp()
        password = self._password(shortcode, passkey, timestamp)

        payload = {
            'BusinessShortCode': shortcode,
            'Password': password,
            'Timestamp': timestamp,
            'CheckoutRequestID': checkout_request_id,
        }

        url = f"{_base_url()}/mpesa/stkpushquery/v1/query"
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("STK Push Query failed: %s", exc)
            raise
