from flask_babel import lazy_gettext as _
import requests
import logging
from .authenticator import (
    BasicAuthenticationProvider,
    PatronData,
)
from .config import (
    CannotLoadConfiguration,
)
from .circulation_exceptions import RemoteInitiatedServerError
import urllib.parse
from core.model import (
    ExternalIntegration,
)

class FirstBookAuthenticationAPI(BasicAuthenticationProvider):

    NAME = 'First Book (deprecated)'

    DESCRIPTION = _("""
        An authentication service for Open eBooks that authenticates
        using access codes and PINs. (This is the old version.)""")

    DISPLAY_NAME = NAME
    DEFAULT_IDENTIFIER_LABEL = _("Access Code")
    LOGIN_BUTTON_IMAGE = "FirstBookLoginButton280.png"

    # If FirstBook sends this message it means they accepted the
    # patron's credentials.
    SUCCESS_MESSAGE = 'Valid Code Pin Pair'

    # Server-side validation happens before the identifier
    # is converted to uppercase, which means lowercase characters
    # are valid.
    DEFAULT_IDENTIFIER_REGULAR_EXPRESSION = '^[A-Za-z0-9@]+$'
    DEFAULT_PASSWORD_REGULAR_EXPRESSION = '^[0-9]+$'

    SETTINGS = [
        { "key": ExternalIntegration.URL, "format": "url", "label": _("URL"), "required": True },
        { "key": ExternalIntegration.PASSWORD, "label": _("Key"), "required": True },
    ] + BasicAuthenticationProvider.SETTINGS

    log = logging.getLogger("First Book authentication API")

    def __init__(self, library_id, integration, analytics=None, root=None):
        super(FirstBookAuthenticationAPI, self).__init__(library_id, integration, analytics)
        if not root:
            url = integration.url
            key = integration.password
            if not (url and key):
                raise CannotLoadConfiguration(
                    "First Book server not configured."
                )
            if '?' in url:
                url += '&'
            else:
                url += '?'
            root = url + 'key=' + key
        self.root = root

    # Begin implementation of BasicAuthenticationProvider abstract
    # methods.

    def remote_authenticate(self, username, password):
        # All FirstBook credentials are in upper-case.
        username = username.upper()

        # If they fail a PIN test, there is no authenticated patron.
        if not self.remote_pin_test(username, password):
            return None

        # FirstBook keeps track of absolutely no information
        # about the patron other than the permanent ID,
        # which is also the authorization identifier.
        return PatronData(
            permanent_id=username,
            authorization_identifier=username,
        )

    # End implementation of BasicAuthenticationProvider abstract methods.

    def remote_pin_test(self, barcode, pin):
        url = self.root + "&accesscode=%s&pin=%s" % tuple(map(
            urllib.parse.quote, (barcode, pin)
        ))
        try:
            response = self.request(url)
        except requests.exceptions.ConnectionError as e:
            raise RemoteInitiatedServerError(
                str(e),
                self.NAME
            )
        content = response.content.decode("utf8")
        if response.status_code != 200:
            msg = "Got unexpected response code %d. Content: %s" % (
                response.status_code, content
            )
            raise RemoteInitiatedServerError(msg, self.NAME)
        if self.SUCCESS_MESSAGE in content:
            return True
        return False

    def request(self, url):
        """Make an HTTP request.

        Defined solely so it can be overridden in the mock.
        """
        return requests.get(url)


# Specify which of the classes defined in this module is the
# authentication provider.
AuthenticationProvider = FirstBookAuthenticationAPI
