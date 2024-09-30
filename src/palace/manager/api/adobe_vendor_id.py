from __future__ import annotations

import base64
import datetime
import sys
import uuid
from typing import Any

import jwt
from jwt.algorithms import HMACAlgorithm
from jwt.exceptions import InvalidIssuedAtError
from sqlalchemy import select
from sqlalchemy.orm import Query
from sqlalchemy.orm.session import Session

from palace.manager.api.config import CannotLoadConfiguration
from palace.manager.api.discovery.opds_registration import OpdsRegistrationService
from palace.manager.integration.goals import Goals
from palace.manager.service.integration_registry.discovery import DiscoveryRegistry
from palace.manager.sqlalchemy.model.credential import Credential
from palace.manager.sqlalchemy.model.datasource import DataSource
from palace.manager.sqlalchemy.model.discovery_service_registration import (
    DiscoveryServiceRegistration,
    RegistrationStatus,
)
from palace.manager.sqlalchemy.model.integration import IntegrationConfiguration
from palace.manager.sqlalchemy.model.library import Library
from palace.manager.sqlalchemy.model.patron import Patron
from palace.manager.util.datetime_helpers import datetime_utc, utc_now
from palace.manager.util.log import LoggerMixin

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self


class AuthdataUtility(LoggerMixin):
    """Generate authdata JWTs as per the Vendor ID Service spec:
    https://docs.google.com/document/d/1j8nWPVmy95pJ_iU4UTC-QgHK2QhDUSdQ0OQTFR2NE_0

    Capable of encoding JWTs (for this library), and decoding them
    (from this library and potentially others).

    Also generates and decodes JWT-like strings used to get around
    Adobe's lack of support for authdata in deactivation.
    """

    # The type of the Credential created to identify a patron to the
    # Vendor ID Service. Using this as an alias keeps the Vendor ID
    # Service from knowing anything about the patron's true
    # identity. This Credential is permanent (unlike a patron's
    # username or authorization identifier), but can be revoked (if
    # the patron needs to reset their Adobe account ID) with no
    # consequences other than losing their currently checked-in books.
    ADOBE_ACCOUNT_ID_PATRON_IDENTIFIER = "Identifier for Adobe account ID purposes"

    ALGORITHM = "HS256"

    def __init__(
        self,
        vendor_id: str,
        library_uri: str,
        library_short_name: str,
        secret: str,
    ) -> None:
        """Basic constructor.

        :param vendor_id: The Adobe Vendor ID that should accompany authdata
        generated by this utility.

        If this library has its own Adobe Vendor ID, it should go
        here. If this library is delegating authdata control to some
        other library, that library's Vendor ID should go here.

        :param library_uri: A URI identifying this library. This is
        used when generating JWTs.

        :param short_name: A short string identifying this
        library. This is used when generating short client tokens,
        which must be as short as possible (thus the name).

        :param secret: A secret used to sign this library's authdata.
        """
        self.vendor_id = vendor_id

        # This is used to _encode_ JWTs and send them to the
        # delegation authority.
        self.library_uri = library_uri

        # This is used to _encode_ short client tokens.
        self.short_name = library_short_name.upper()

        # This is used to encode both JWTs and short client tokens.
        self.secret = secret

        self.short_token_signer = HMACAlgorithm(HMACAlgorithm.SHA256)
        self.short_token_signing_key = self.short_token_signer.prepare_key(self.secret)

    @classmethod
    def from_config(cls, library: Library, _db: Session | None = None) -> Self | None:
        """Initialize an AuthdataUtility from site configuration.

        The library must be successfully registered with a discovery
        integration in order for that integration to be a candidate
        to provide configuration for the AuthdataUtility.

        :return: An AuthdataUtility if one is configured; otherwise None.

        :raise CannotLoadConfiguration: If an AuthdataUtility is
            incompletely configured.
        """
        _db = _db or Session.object_session(library)
        if not _db:
            raise ValueError(
                "No database connection provided and could not derive one from Library object!"
            )
        # Use a version of the library
        library = _db.merge(library, load=False)

        # Find the first registration that has a vendor ID.
        protocol = DiscoveryRegistry().get_protocol(OpdsRegistrationService)
        registration = _db.scalars(
            select(DiscoveryServiceRegistration)
            .join(IntegrationConfiguration)
            .where(
                DiscoveryServiceRegistration.library == library,
                DiscoveryServiceRegistration.vendor_id != None,
                DiscoveryServiceRegistration.status == RegistrationStatus.SUCCESS,
                IntegrationConfiguration.protocol == protocol,
                IntegrationConfiguration.goal == Goals.DISCOVERY_GOAL,
            )
        ).first()

        if registration is None:
            # No vendor ID is configured for this library.
            return None

        library_uri = library.settings.website
        vendor_id = registration.vendor_id
        short_name = registration.short_name
        shared_secret = registration.shared_secret

        if not vendor_id or not library_uri or not short_name or not shared_secret:
            raise CannotLoadConfiguration(
                "Short Client Token configuration is incomplete. "
                "vendor_id (%s), username (%s), password (%s) and "
                "Library website_url (%s) must all be defined."
                % (vendor_id, library_uri, short_name, shared_secret)
            )
        if "|" in short_name:
            raise CannotLoadConfiguration(
                "Library short name cannot contain the pipe character."
            )
        return cls(vendor_id, library_uri, short_name, shared_secret)

    @classmethod
    def adobe_relevant_credentials(self, patron: Patron) -> Query[Credential]:
        """Find all Adobe-relevant Credential objects for the given
        patron.

        :return: A SQLAlchemy query
        """
        _db = Session.object_session(patron)
        return (
            _db.query(Credential)
            .filter(Credential.patron == patron)
            .filter(
                Credential.type == AuthdataUtility.ADOBE_ACCOUNT_ID_PATRON_IDENTIFIER
            )
        )

    def encode(self, patron_identifier: str | None) -> tuple[str, bytes]:
        """Generate an authdata JWT suitable for putting in an OPDS feed, where
        it can be picked up by a client and sent to the delegation
        authority to look up an Adobe ID.

        :return: A 2-tuple (vendor ID, authdata)
        """
        if not patron_identifier:
            raise ValueError("No patron identifier specified")

        # pyjwt >2.6.0 does not validate tokens created in the same second
        # so we create a token from 1 second in the past
        now = utc_now() - datetime.timedelta(seconds=1)
        expires = now + datetime.timedelta(minutes=60)
        authdata = self._encode(self.library_uri, patron_identifier, now, expires)
        return self.vendor_id, authdata

    def _encode(
        self,
        iss: str,
        sub: str | None = None,
        iat: datetime.datetime | None = None,
        exp: datetime.datetime | None = None,
    ) -> bytes:
        """Helper method split out separately for use in tests."""
        payload: dict[str, Any] = dict(iss=iss)  # Issuer
        if sub:
            payload["sub"] = sub  # Subject
        if iat:
            payload["iat"] = self.numericdate(iat)  # Issued At
        if exp:
            payload["exp"] = self.numericdate(exp)  # Expiration Time
        return base64.encodebytes(
            bytes(
                jwt.encode(payload, self.secret, algorithm=self.ALGORITHM),
                encoding="utf-8",
            )
        )

    @classmethod
    def adobe_base64_encode(cls, str_to_encode: str | bytes) -> str:
        """A modified base64 encoding that avoids triggering an Adobe bug.

        The bug seems to happen when the 'password' portion of a
        username/password pair contains a + character. So we replace +
        with :. We also replace / (another "suspicious" character)
        with ;. and strip newlines.
        """
        if isinstance(str_to_encode, str):
            str_to_encode = str_to_encode.encode("utf-8")
        encoded = base64.encodebytes(str_to_encode).decode("utf-8").strip()
        return encoded.replace("+", ":").replace("/", ";").replace("=", "@")

    @classmethod
    def adobe_base64_decode(cls, str_to_decode: str) -> bytes:
        """Undoes adobe_base64_encode."""
        encoded = str_to_decode.replace(":", "+").replace(";", "/").replace("@", "=")
        return base64.decodebytes(encoded.encode("utf-8"))

    def decode(self, authdata: bytes) -> tuple[str, str]:
        """Decode and verify an authdata JWT from one of the libraries managed
        by `secrets_by_library`.

        :return: a 2-tuple (library_uri, patron_identifier)

        :raise jwt.exceptions.DecodeError: When the JWT is not valid
            for any reason.
        """

        self.log.info("Authdata.decode() received authdata %s", authdata)
        # We are going to try to verify the authdata as is (in case
        # Adobe secretly decoded it en route), but we're also going to
        # try to decode it ourselves and verify it that way.
        potential_tokens = [authdata]
        try:
            decoded = base64.decodebytes(authdata)
            potential_tokens.append(decoded)
        except Exception as e:
            # Do nothing -- the authdata was not encoded to begin with.
            pass

        exceptions = []
        for authdata in potential_tokens:
            try:
                return self._decode(authdata)
            except Exception as e:
                self.log.error("Error decoding %s", authdata, exc_info=e)
                exceptions.append(e)

        # If we got to this point there is at least one exception
        # in the list.
        raise exceptions[-1]

    def _decode(self, authdata: bytes) -> tuple[str, str]:
        # First, decode the authdata without checking the signature.
        authdata_str = authdata.decode("utf-8")
        decoded = jwt.decode(
            authdata_str,
            algorithms=[self.ALGORITHM],
            options=dict(verify_signature=False, verify_exp=True),
        )

        # Fail future JWTs as per requirements, pyJWT stopped doing this, so doing it manually
        if "iat" in decoded and decoded["iat"] > self.numericdate(utc_now()):
            raise InvalidIssuedAtError("Issued At claim (iat) cannot be in the future")

        # This lets us get the library URI, which lets us get the secret.
        library_uri = decoded.get("iss")
        if library_uri != self.library_uri:
            # The request came in without a library specified
            # or with an unknown library specified.
            raise jwt.exceptions.DecodeError("Unknown library: %s" % library_uri)

        # We know the secret for this library, so we can re-decode the
        # secret and require signature validation this time.
        secret = self.secret
        decoded = jwt.decode(authdata_str, secret, algorithms=[self.ALGORITHM])
        if "sub" not in decoded:
            raise jwt.exceptions.DecodeError("No subject specified.")
        return library_uri, decoded["sub"]

    @classmethod
    def _adobe_patron_identifier(cls, patron: Patron) -> str | None:
        """Take patron object and return identifier for Adobe ID purposes"""
        _db = Session.object_session(patron)
        internal = DataSource.lookup(_db, DataSource.INTERNAL_PROCESSING)

        def refresh(credential: Credential) -> None:
            credential.credential = str(uuid.uuid1())

        patron_identifier = Credential.lookup(
            _db,
            internal,
            AuthdataUtility.ADOBE_ACCOUNT_ID_PATRON_IDENTIFIER,
            patron,
            refresher_method=refresh,
            allow_persistent_token=True,
        )
        return patron_identifier.credential

    def short_client_token_for_patron(
        self, patron_information: Patron | str
    ) -> tuple[str, str]:
        """Generate short client token for patron, or for a patron's identifier
        for Adobe ID purposes"""

        if isinstance(patron_information, Patron):
            # Find the patron's identifier for Adobe ID purposes.
            patron_identifier = self._adobe_patron_identifier(patron_information)
        else:
            patron_identifier = patron_information

        vendor_id, token = self.encode_short_client_token(patron_identifier)
        return vendor_id, token

    def _now(self) -> datetime.datetime:
        """Function to return current time. Used to override in testing."""
        return utc_now()

    def encode_short_client_token(
        self, patron_identifier: str | None, expires: dict[str, int] | None = None
    ) -> tuple[str, str]:
        """Generate a short client token suitable for putting in an OPDS feed,
        where it can be picked up by a client and sent to the
        delegation authority to look up an Adobe ID.

        :return: A 2-tuple (vendor ID, token)
        """
        if expires is None:
            expires = {"minutes": 60}
        if not patron_identifier:
            raise ValueError("No patron identifier specified")
        expires_timestamp = int(
            self.numericdate(self._now() + datetime.timedelta(**expires))
        )
        authdata = self._encode_short_client_token(
            self.short_name, patron_identifier, expires_timestamp
        )
        return self.vendor_id, authdata

    def _encode_short_client_token(
        self,
        library_short_name: str,
        patron_identifier: str,
        expires: int | float,
    ) -> str:
        base = library_short_name + "|" + str(expires) + "|" + patron_identifier
        signature = self.short_token_signer.sign(
            base.encode("utf-8"), self.short_token_signing_key
        )
        signature_encoded = self.adobe_base64_encode(signature)
        if len(base) > 80:
            self.log.error(
                "Username portion of short client token exceeds 80 characters; Adobe will probably truncate it."
            )
        if len(signature_encoded) > 76:
            self.log.error(
                "Password portion of short client token exceeds 76 characters; Adobe will probably truncate it."
            )
        return base + "|" + signature_encoded

    def decode_short_client_token(self, token: str) -> tuple[str, str]:
        """Attempt to interpret a 'username' and 'password' as a short
        client token identifying a patron of a specific library.

        :return: a 2-tuple (library_uri, patron_identifier)

        :raise ValueError: When the token is not valid for any reason.
        """
        if "|" not in token:
            raise ValueError(
                'Supposed client token "%s" does not contain a pipe.' % token
            )

        username, password = token.rsplit("|", 1)
        return self.decode_two_part_short_client_token(username, password)

    def decode_two_part_short_client_token(
        self, username: str, password: str
    ) -> tuple[str, str]:
        """Decode a short client token that has already been split into
        two parts.
        """
        signature = self.adobe_base64_decode(password)
        return self._decode_short_client_token(username, signature)

    def _decode_short_client_token(
        self, token: str, supposed_signature: bytes
    ) -> tuple[str, str]:
        """Make sure a client token is properly formatted, correctly signed,
        and not expired.
        """
        if token.count("|") < 2:
            raise ValueError("Invalid client token: %s" % token)
        library_short_name, expiration_str, patron_identifier = token.split("|", 2)

        library_short_name = library_short_name.upper()
        try:
            expiration = float(expiration_str)
        except ValueError:
            raise ValueError('Expiration time "%s" is not numeric.' % expiration_str)

        # We don't police the content of the patron identifier but there
        # has to be _something_ there.
        if not patron_identifier:
            raise ValueError("Token %s has empty patron identifier" % token)

        if library_short_name != self.short_name:
            raise ValueError(
                'I don\'t know how to handle tokens from library "%s"'
                % library_short_name
            )

        # Don't bother checking an expired token.
        now = utc_now()
        expiration_datetime = self.EPOCH + datetime.timedelta(seconds=expiration)
        if expiration_datetime < now:
            raise ValueError(
                f"Token {token} expired at {expiration_datetime} (now is {now})."
            )

        # Sign the token and check against the provided signature.
        key = self.short_token_signer.prepare_key(self.secret)
        actual_signature = self.short_token_signer.sign(token.encode("utf-8"), key)

        if actual_signature != supposed_signature:
            raise ValueError("Invalid signature for %s." % token)

        return self.library_uri, patron_identifier

    EPOCH = datetime_utc(1970, 1, 1)

    @classmethod
    def numericdate(cls, d: datetime.datetime) -> float:
        """Turn a datetime object into a NumericDate as per RFC 7519."""
        return (d - cls.EPOCH).total_seconds()
