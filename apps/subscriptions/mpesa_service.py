"""
apps/subscriptions/mpesa_service.py
=====================================
Safaricom Daraja API wrapper — Lipa Na M-Pesa Online (STK Push).

What this module does
---------------------
Provides ``MpesaService``, a thin stateless class that wraps the two Daraja
v1 endpoints used for subscription payments:

1. **OAuth token** — ``POST /oauth/v1/generate``
   Fetches a short-lived Bearer token using the app's consumer key + secret.
   Tokens are cached in Django's cache backend for 50 minutes (Safaricom
   tokens expire after 60 minutes) so that multiple concurrent payments do not
   each trigger a redundant round-trip.

2. **STK Push** — ``POST /mpesa/stkpush/v1/processrequest``
   Sends a payment prompt to the customer's phone.  The user is asked to enter
   their M-Pesa PIN on their handset; Safaricom then POSTs the result to our
   callback URL asynchronously.

3. **STK Push Query** — ``POST /mpesa/stkpushquery/v1/query``
   Polls the status of a previous STK Push when the callback has not been
   delivered yet (common in development where the callback URL is not publicly
   reachable).

Sandbox vs production
---------------------
Set ``MPESA_SANDBOX = True`` in Django settings (or the ``.env`` file) to route
all requests to ``https://sandbox.safaricom.co.ke``.  The sandbox uses
Safaricom's public test shortcode (``174379``) and passkey which are pre-loaded
in ``config/settings/base.py`` as defaults.

For development without a public callback URL, expose your local server via
ngrok (or a similar tunnelling tool) and set ``MPESA_CALLBACK_URL`` to the
resulting HTTPS URL::

    MPESA_CALLBACK_URL=https://<your-id>.ngrok.io/subscriptions/mpesa/callback/

Amount in sandbox
-----------------
Set ``MPESA_SANDBOX=True`` and the view layer will send **KES 1** to Daraja
regardless of the actual plan price.  This is intentional — Safaricom's sandbox
test SIM only reliably processes KES 1, and paying 2,500 on a sandbox account
would charge real money.

Required settings
-----------------
All settings are read from Django ``settings`` at call-time (not module import
time) so they can be overridden per environment:

+---------------------------+------------------------------------------+
| Setting                   | Description                              |
+===========================+==========================================+
| ``MPESA_SANDBOX``         | ``True`` → sandbox, ``False`` → prod     |
+---------------------------+------------------------------------------+
| ``MPESA_CONSUMER_KEY``    | App consumer key from Daraja portal      |
+---------------------------+------------------------------------------+
| ``MPESA_CONSUMER_SECRET`` | App consumer secret from Daraja portal   |
+---------------------------+------------------------------------------+
| ``MPESA_SHORTCODE``       | Business/PayBill shortcode               |
+---------------------------+------------------------------------------+
| ``MPESA_PASSKEY``         | Lipa Na M-Pesa Online passkey            |
+---------------------------+------------------------------------------+
| ``MPESA_CALLBACK_URL``    | Publicly reachable HTTPS callback URL    |
+---------------------------+------------------------------------------+

Phone number formats accepted
------------------------------
``normalize_phone()`` accepts any of:

* ``07XXXXXXXX``      — local format with leading zero
* ``7XXXXXXXX``       — without leading zero
* ``+2547XXXXXXXX``   — international with plus sign
* ``2547XXXXXXXX``    — international without plus sign

All are converted to the ``2547XXXXXXXX`` (12-digit) format that Daraja
requires.  Numbers that cannot be normalised into that format raise
``ValueError``.

Error handling
--------------
Network failures (timeouts, HTTP 4xx/5xx) are logged at ERROR level and
re-raised so callers can catch them and mark the transaction as failed.
The caller is responsible for updating the ``MpesaTransaction`` status.
"""

import base64
import logging
import re
from datetime import datetime

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_SANDBOX_BASE    = 'https://sandbox.safaricom.co.ke'
_PRODUCTION_BASE = 'https://api.safaricom.co.ke'

#: Compiled regex for validated Kenyan Safaricom numbers in international form.
#: Accepts 2547XXXXXXXX (Safaricom 07xx) and 2541XXXXXXXX (Safaricom 01xx).
_PHONE_RE = re.compile(r'^2547\d{8}$|^2541\d{8}$')

#: Django cache key used to persist the Daraja access token between requests.
_TOKEN_CACHE_KEY = 'mpesa_access_token'

#: How long to cache the access token.  Daraja tokens expire at 60 minutes;
#: we evict ours at 50 to provide a safe margin before expiry.
_TOKEN_TTL = 50 * 60  # seconds


def _base_url() -> str:
    """Return the Daraja base URL for the current environment."""
    return _SANDBOX_BASE if getattr(settings, 'MPESA_SANDBOX', True) else _PRODUCTION_BASE


# ---------------------------------------------------------------------------
# MpesaService
# ---------------------------------------------------------------------------

class MpesaService:
    """
    Stateless service class for Safaricom Daraja Lipa Na M-Pesa Online.

    Instantiate once per request (or per-call — it holds no mutable state).
    All configuration is read from ``django.conf.settings`` at call time.

    Usage example::

        service = MpesaService()

        # Validate and normalise phone before touching Daraja
        phone = service.normalize_phone('0712345678')   # → '254712345678'

        # Initiate a payment prompt on the customer's phone
        result = service.lipa_na_mpesa_online(
            phone_number=phone,
            amount=1,                        # KES 1 in sandbox
            account_reference='AnalyticsMeta',
            transaction_desc='Plan Upgrade',
        )
        checkout_id = result['CheckoutRequestID']

        # Later (or on every status-poll iteration):
        status = service.query_stk_push(checkout_id)
    """

    # ------------------------------------------------------------------ #
    # OAuth token
    # ------------------------------------------------------------------ #

    def get_access_token(self) -> str:
        """
        Return a valid Daraja Bearer token, fetching a fresh one if needed.

        The token is stored in Django's default cache backend under
        ``'mpesa_access_token'`` for ``_TOKEN_TTL`` seconds (50 min).
        On a cache hit the OAuth endpoint is not called, saving one HTTP
        round-trip per payment initiation.

        Raises
        ------
        ValueError
            If ``MPESA_CONSUMER_KEY`` or ``MPESA_CONSUMER_SECRET`` are empty.
        requests.RequestException
            On any network-level failure talking to the OAuth endpoint.

        Returns
        -------
        str
            A non-empty Bearer token string.
        """
        cached = cache.get(_TOKEN_CACHE_KEY)
        if cached:
            return cached

        consumer_key    = settings.MPESA_CONSUMER_KEY
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
            cache.set(_TOKEN_CACHE_KEY, token, _TOKEN_TTL)
            return token
        except requests.RequestException as exc:
            logger.error("M-Pesa OAuth request failed: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    # Static helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _timestamp() -> str:
        """
        Return the current local time as a ``YYYYMMDDHHMMSS`` string.

        Daraja requires this exact format in both the STK Push request body
        and the Base64 password derivation.  East Africa Time (UTC+3) is
        correct for production; the sandbox accepts any recent timestamp.
        """
        return datetime.now().strftime('%Y%m%d%H%M%S')

    @staticmethod
    def _password(shortcode: str, passkey: str, timestamp: str) -> str:
        """
        Derive the Daraja STK Push ``Password`` field.

        The password is the Base64 encoding of the concatenation of the
        business shortcode, the Lipa Na M-Pesa Online passkey, and the
        current timestamp::

            password = base64( shortcode + passkey + timestamp )

        Parameters
        ----------
        shortcode
            The M-Pesa business short code (e.g. ``'174379'`` for sandbox).
        passkey
            The Lipa Na M-Pesa Online passkey from the Daraja portal.
        timestamp
            Timestamp string from ``_timestamp()`` in ``YYYYMMDDHHMMSS`` form.

        Returns
        -------
        str
            Base64-encoded password string, ready to drop into the request body.
        """
        raw = f"{shortcode}{passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """
        Normalise a Kenyan mobile number to Safaricom's ``2547XXXXXXXX`` format.

        Accepted input formats
        ----------------------
        * ``07XXXXXXXX``    — local with leading zero
        * ``7XXXXXXXX``     — local without leading zero (Safaricom 07xx lines)
        * ``01XXXXXXXX``    — local with leading zero (Safaricom 01xx lines)
        * ``1XXXXXXXX``     — local without leading zero (Safaricom 01xx lines)
        * ``+2547XXXXXXXX`` — E.164 with plus prefix
        * ``+2541XXXXXXXX`` — E.164 with plus prefix (01xx)
        * ``2547XXXXXXXX``  — already in target format
        * ``2541XXXXXXXX``  — already in target format

        Parameters
        ----------
        phone
            Raw phone number string from user input.  Leading/trailing
            whitespace and hyphens are stripped automatically.

        Returns
        -------
        str
            12-digit string in ``2547XXXXXXXX`` or ``2541XXXXXXXX`` format,
            e.g. ``'254712345678'``.

        Raises
        ------
        ValueError
            If the number cannot be normalised into a recognised Safaricom
            format.  The exception message is safe to surface to the user.

        Examples
        --------
        >>> MpesaService.normalize_phone('0712345678')
        '254712345678'
        >>> MpesaService.normalize_phone('+254712345678')
        '254712345678'
        >>> MpesaService.normalize_phone('712345678')
        '254712345678'
        """
        phone = phone.strip().replace(' ', '').replace('-', '')

        if phone.startswith('+254'):
            phone = '254' + phone[4:]
        elif phone.startswith('0'):
            phone = '254' + phone[1:]
        elif phone.startswith('7') or phone.startswith('1'):
            phone = '254' + phone

        if not _PHONE_RE.match(phone):
            raise ValueError(
                f"Invalid Kenyan phone number: {phone!r}. "
                "Expected format: 07XXXXXXXX, +2547XXXXXXXX, or 2547XXXXXXXX."
            )
        return phone

    # ------------------------------------------------------------------ #
    # STK Push — initiate payment
    # ------------------------------------------------------------------ #

    def lipa_na_mpesa_online(
        self,
        phone_number: str,
        amount: int,
        account_reference: str,
        transaction_desc: str,
    ) -> dict:
        """
        Dispatch a Lipa Na M-Pesa Online (STK Push) payment request.

        This sends a push notification to the customer's phone asking them to
        confirm the payment by entering their M-Pesa PIN.  The method returns
        immediately with Daraja's acknowledgement; the actual payment result
        arrives later via the callback URL.

        The caller should:

        1. Create a ``MpesaTransaction`` row with ``status='pending'`` *before*
           calling this method.
        2. Store ``result['CheckoutRequestID']`` on the transaction row.
        3. Either wait for the callback to update the transaction, or poll
           ``query_stk_push()`` on each frontend status-poll request.

        Parameters
        ----------
        phone_number
            Customer's phone number.  Any accepted format (see
            ``normalize_phone``); the method normalises it internally.
        amount
            Integer KES amount.  Daraja rejects decimal values.  Pass ``1``
            during sandbox testing (see module docstring).
        account_reference
            Text shown to the customer on their phone (max 12 characters).
            Longer strings are silently truncated.
        transaction_desc
            Short description of the transaction (max 13 characters).
            Longer strings are silently truncated.

        Returns
        -------
        dict
            Raw JSON response from Daraja.  On success, contains at minimum:

            * ``ResponseCode``       — ``'0'`` means the request was accepted
            * ``CheckoutRequestID``  — used for polling / callback matching
            * ``MerchantRequestID``  — Safaricom's internal reference
            * ``CustomerMessage``    — human-readable confirmation to show user

        Raises
        ------
        ValueError
            If ``phone_number`` fails validation (see ``normalize_phone``).
        requests.RequestException
            On network timeout or HTTP error from Daraja.

        Daraja response codes
        ---------------------
        * ``ResponseCode == '0'``  → STK Push accepted; wait for callback.
        * ``ResponseCode != '0'``  → Request rejected (check
          ``ResponseDescription`` for the reason).

        Notes
        -----
        ``AccountReference`` and ``TransactionDesc`` are truncated to
        Safaricom's limits (12 and 13 characters respectively) to avoid
        silent API rejections.
        """
        token     = self.get_access_token()
        shortcode = settings.MPESA_SHORTCODE
        passkey   = settings.MPESA_PASSKEY
        callback  = settings.MPESA_CALLBACK_URL

        timestamp = self._timestamp()
        password  = self._password(shortcode, passkey, timestamp)
        phone     = self.normalize_phone(phone_number)

        payload = {
            'BusinessShortCode': shortcode,
            'Password':          password,
            'Timestamp':         timestamp,
            'TransactionType':   'CustomerPayBillOnline',
            'Amount':            int(amount),
            'PartyA':            phone,
            'PartyB':            shortcode,
            'PhoneNumber':       phone,
            'CallBackURL':       callback,
            'AccountReference':  account_reference[:12],
            'TransactionDesc':   transaction_desc[:13],
        }

        url     = f"{_base_url()}/mpesa/stkpush/v1/processrequest"
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("STK Push request failed: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    # STK Push Query — check payment status
    # ------------------------------------------------------------------ #

    def query_stk_push(self, checkout_request_id: str) -> dict:
        """
        Query the outcome of a previously dispatched STK Push.

        Called by ``mpesa_payment_status`` on every frontend poll cycle when
        the transaction is still in ``'pending'`` state and the callback has
        not yet arrived (e.g. because the callback URL is not publicly
        reachable in development, or due to Safaricom delivery delays).

        Parameters
        ----------
        checkout_request_id
            The ``CheckoutRequestID`` string returned by
            ``lipa_na_mpesa_online()`` and stored on the
            ``MpesaTransaction`` row.

        Returns
        -------
        dict
            Raw Daraja JSON response.  Key fields the caller inspects:

            * ``ResponseCode``  — ``'0'`` means the API query was accepted
              (not that the payment succeeded).
            * ``ResultCode``    — the *payment* outcome:

              - ``'0'``    → payment successful
              - ``'1032'`` → pending / user has not responded yet
              - ``'1'``    → still being processed by Safaricom
              - other      → payment failed (see ``ResultDesc`` for reason)

            * ``ResultDesc``    — human-readable description of the outcome.

        Raises
        ------
        requests.RequestException
            On network timeout or HTTP error from Daraja.

        Important
        ---------
        ``ResponseCode`` and ``ResultCode`` are **different things**:

        * ``ResponseCode`` is the Daraja *API* response — did the HTTP call
          succeed and did Safaricom understand our request?
        * ``ResultCode`` is the *payment* result — did the customer pay?

        Always check ``ResponseCode == '0'`` before interpreting
        ``ResultCode``.  If ``ResponseCode != '0'``, the query itself failed
        (e.g. auth error, malformed request) and ``ResultCode`` is meaningless.
        """
        token     = self.get_access_token()
        shortcode = settings.MPESA_SHORTCODE
        passkey   = settings.MPESA_PASSKEY
        timestamp = self._timestamp()
        password  = self._password(shortcode, passkey, timestamp)

        payload = {
            'BusinessShortCode': shortcode,
            'Password':          password,
            'Timestamp':         timestamp,
            'CheckoutRequestID': checkout_request_id,
        }

        url     = f"{_base_url()}/mpesa/stkpushquery/v1/query"
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("STK Push Query failed: %s", exc)
            raise
