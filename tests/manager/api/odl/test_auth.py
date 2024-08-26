import json
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from freezegun import freeze_time
from typing_extensions import Self

from palace.manager.api.odl.auth import ODLAuthenticatedGet, TokenTuple
from palace.manager.api.odl.settings import OPDS2AuthType
from palace.manager.core.exceptions import IntegrationException, PalaceValueError
from palace.manager.util.datetime_helpers import utc_now
from palace.manager.util.http import HTTP, BearerAuth
from tests.mocks.mock import MockRequestsResponse


class MockODLAuthenticatedGet(ODLAuthenticatedGet):
    def __init__(
        self, username: str, password: str, auth_type: OPDS2AuthType, feed_url: str
    ) -> None:
        self.username = username
        self.password = password
        self.auth_type = auth_type
        self.feed_url = feed_url
        super().__init__()

    @property
    def _username(self) -> str:
        return self.username

    @property
    def _password(self) -> str:
        return self.password

    @property
    def _auth_type(self) -> OPDS2AuthType:
        return self.auth_type

    @property
    def _feed_url(self) -> str:
        return self.feed_url


class AuthenticatedGetFixture:
    def __init__(self, request_with_timeout: MagicMock) -> None:
        self.username = "username"
        self.password = "password"
        self.token = "token"
        self.feed_url = "http://example.com/feed"
        self.auth_url = "http://authenticate.example.com"
        self.request_url = "http://example.com/123"
        self.headers = {"header": "value"}
        self.authenticated_get = partial(
            MockODLAuthenticatedGet,
            username=self.username,
            password=self.password,
            feed_url=self.feed_url,
            auth_type=OPDS2AuthType.OAUTH,
        )
        self.request_with_timeout = request_with_timeout

        self.responses = {
            "auth_document_401": MockRequestsResponse(
                401,
                {"Content-Type": "application/vnd.opds.authentication.v1.0+json"},
                json.dumps(self.auth_document),
            ),
            "other_401": MockRequestsResponse(
                401,
                {"Content-Type": "text/plain"},
                "Unauthorized",
            ),
            "token_grant": MockRequestsResponse(200, {}, json.dumps(self.token_grant)),
            "data": MockRequestsResponse(200, {}, "Data"),
        }

        self.valid_token = TokenTuple(
            self.token,
            utc_now() + timedelta(seconds=3600),
        )
        self.expired_token = TokenTuple(
            "expired_token",
            utc_now() - timedelta(seconds=1),
        )

        self.request_with_timeout_calls = {
            "feed_url_no_auth": partial(
                call,
                "GET",
                self.feed_url,
            ),
            "token_grant": partial(
                call,
                "POST",
                self.auth_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
                auth=(
                    self.username,
                    self.password,
                ),
                allowed_response_codes=["2xx"],
            ),
            "request_with_token": partial(
                call,
                "GET",
                self.request_url,
                headers=self.headers,
                auth=BearerAuth(self.token),
            ),
        }

        self.request_with_token_call = partial(
            call,
            "GET",
            self.request_url,
            headers=self.headers,
            auth=BearerAuth(self.token),
        )

    def initialize_authenticated_get(
        self,
        authenticated_get: MockODLAuthenticatedGet | None = None,
        *,
        expired: bool = False
    ) -> MockODLAuthenticatedGet:
        # Set the token url and session token so that the authenticated_get can make requests
        # without first going through the refresh process
        if authenticated_get is None:
            authenticated_get = self.authenticated_get()
        authenticated_get._token_url = self.auth_url
        authenticated_get._session_token = (
            self.valid_token if not expired else self.expired_token
        )
        return authenticated_get

    @property
    def auth_document(self) -> dict[str, Any]:
        return {
            "id": "http://example.com/auth",
            "title": "Authentication Document",
            "authentication": [
                {
                    "type": "http://opds-spec.org/auth/oauth/client_credentials",
                    "links": [
                        {
                            "rel": "authenticate",
                            "href": self.auth_url,
                        },
                    ],
                }
            ],
        }

    @property
    def token_grant(self) -> dict[str, Any]:
        return {
            "access_token": self.token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    @classmethod
    @contextmanager
    def fixture(cls) -> Generator[Self, None, None]:
        with patch.object(HTTP, "request_with_timeout") as mock_request_with_timeout:
            yield cls(mock_request_with_timeout)


@pytest.fixture
def authenticated_get_fixture() -> Generator[AuthenticatedGetFixture, None, None]:
    with AuthenticatedGetFixture.fixture() as fixture:
        yield fixture


class TestODLAuthenticatedGet:
    def test__basic_auth_get(
        self, authenticated_get_fixture: AuthenticatedGetFixture
    ) -> None:
        mock_request_with_timeout = authenticated_get_fixture.request_with_timeout
        authenticated_get = authenticated_get_fixture.authenticated_get(
            auth_type=OPDS2AuthType.BASIC
        )
        response = authenticated_get._get(
            authenticated_get_fixture.request_url, authenticated_get_fixture.headers
        )
        assert response == mock_request_with_timeout.return_value
        mock_request_with_timeout.assert_called_once_with(
            "GET",
            authenticated_get_fixture.request_url,
            headers=authenticated_get_fixture.headers,
            auth=(
                authenticated_get_fixture.username,
                authenticated_get_fixture.password,
            ),
        )

    def test__no_auth_get(
        self, authenticated_get_fixture: AuthenticatedGetFixture
    ) -> None:
        mock_request_with_timeout = authenticated_get_fixture.request_with_timeout
        authenticated_get = authenticated_get_fixture.authenticated_get(
            auth_type=OPDS2AuthType.NONE
        )
        response = authenticated_get._get(
            authenticated_get_fixture.request_url, authenticated_get_fixture.headers
        )
        assert response == mock_request_with_timeout.return_value
        mock_request_with_timeout.assert_called_once_with(
            "GET",
            authenticated_get_fixture.request_url,
            headers=authenticated_get_fixture.headers,
        )

    def test__unknown_auth_type(
        self, authenticated_get_fixture: AuthenticatedGetFixture
    ) -> None:
        authenticated_get = authenticated_get_fixture.authenticated_get(
            auth_type="invalid"  # type: ignore[arg-type]
        )
        with pytest.raises(PalaceValueError) as exc_info:
            authenticated_get._get(
                authenticated_get_fixture.request_url, authenticated_get_fixture.headers
            )
        assert str(exc_info.value) == "Invalid OPDS2AuthType: 'invalid'"

    @pytest.mark.parametrize(
        "authentication,expected",
        [
            pytest.param(
                [
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                        "links": [
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate.example.com",
                            },
                        ],
                    },
                ],
                "http://authenticate.example.com",
                id="valid",
            ),
            pytest.param(
                [
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                        "links": [
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate.example.com",
                            },
                        ],
                    },
                    {
                        "type": "http://opds-spec.org/auth/basic",
                        "links": [
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate2.example.com",
                            },
                        ],
                    },
                ],
                "http://authenticate.example.com",
                id="multiple different",
            ),
            pytest.param(
                [
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                        "links": [
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate.example.com",
                            },
                        ],
                    },
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                        "links": [
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate3.example.com",
                            },
                        ],
                    },
                ],
                IntegrationException,
                id="multiple same",
            ),
            pytest.param([], IntegrationException, id="empty"),
            pytest.param(
                [
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                    },
                ],
                IntegrationException,
                id="missing links",
            ),
            pytest.param(
                [
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                        "links": [],
                    },
                ],
                IntegrationException,
                id="empty links",
            ),
            pytest.param(
                [
                    {
                        "type": "http://opds-spec.org/auth/oauth/client_credentials",
                        "links": [
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate.example.com",
                            },
                            {
                                "rel": "authenticate",
                                "href": "http://authenticate2.example.com",
                            },
                        ],
                    },
                ],
                IntegrationException,
                id="multiple links",
            ),
        ],
    )
    def test__get_oauth_url_from_auth_document(
        self,
        authenticated_get_fixture: AuthenticatedGetFixture,
        authentication: list[dict[str, Any]],
        expected: type[Exception] | str,
    ) -> None:
        auth_document = authenticated_get_fixture.auth_document
        auth_document["authentication"] = authentication
        context = (
            nullcontext() if isinstance(expected, str) else pytest.raises(expected)
        )

        with context:
            assert (
                MockODLAuthenticatedGet._get_oauth_url_from_auth_document(
                    json.dumps(auth_document)
                )
                == expected
            )

    @pytest.mark.parametrize(
        "data,expected",
        [
            ("{}", IntegrationException),
            ('{"access_token":"token"}', IntegrationException),
            ('{"token_type":"Bearer"}', IntegrationException),
            ('{"expires_in":3600}', IntegrationException),
            (
                '{"access_token":"token", "token_type":"invalid", "expires_in":3600}',
                IntegrationException,
            ),
            (
                '{"access_token":"token", "token_type":"Bearer", "expires_in":-320}',
                IntegrationException,
            ),
            ('{"access_token":"token","token_type":"Bearer"}', IntegrationException),
            (
                '{"access_token":"token","token_type":"Bearer","expires_in":3600}',
                TokenTuple(
                    "token",
                    datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=3600),
                ),
            ),
        ],
    )
    @freeze_time("2021-01-01")
    def test__oauth_session_token_refresh(
        self,
        authenticated_get_fixture: AuthenticatedGetFixture,
        data: str,
        expected: TokenTuple | type[Exception],
    ) -> None:
        mock_request_with_timeout = authenticated_get_fixture.request_with_timeout
        mock_request_with_timeout.return_value = MockRequestsResponse(200, {}, data)
        context = (
            nullcontext()
            if isinstance(expected, TokenTuple)
            else pytest.raises(expected)
        )

        with context:
            token = MockODLAuthenticatedGet._oauth_session_token_refresh(
                authenticated_get_fixture.auth_url,
                authenticated_get_fixture.username,
                authenticated_get_fixture.password,
            )
            assert token == expected
        assert mock_request_with_timeout.call_count == 1
        mock_request_with_timeout.assert_has_calls(
            [authenticated_get_fixture.request_with_timeout_calls["token_grant"]()]
        )

    def test__oauth_get_failed_auth_document_request(
        self, authenticated_get_fixture: AuthenticatedGetFixture
    ) -> None:
        """
        If the auth document request fails, an exception is raised.
        """
        mock_request_with_timeout = authenticated_get_fixture.request_with_timeout
        mock_request_with_timeout.return_value = (
            authenticated_get_fixture.responses.get("other_401")
        )
        with pytest.raises(IntegrationException) as exc_info:
            authenticated_get_fixture.authenticated_get()._get(
                authenticated_get_fixture.request_url, authenticated_get_fixture.headers
            )
        assert "Unable to fetch OPDS authentication document" in str(exc_info.value)

    @pytest.mark.parametrize(
        "responses,calls,initialized,expired",
        [
            pytest.param(
                ["auth_document_401", "token_grant", "data"],
                [
                    "feed_url_no_auth",
                    "token_grant",
                    "request_with_token",
                ],
                False,
                False,
                id="first request - full token refresh",
            ),
            pytest.param(
                ["data"],
                ["request_with_token"],
                True,
                False,
                id="second request - token already initialized - directly make request",
            ),
            pytest.param(
                ["token_grant", "data"],
                [
                    "token_grant",
                    "request_with_token",
                ],
                True,
                True,
                id="expired token - do refresh with already known url",
            ),
            pytest.param(
                ["token_grant", "other_401"],
                [
                    "token_grant",
                    "request_with_token",
                ],
                True,
                True,
                id="already refreshed, still 401 response - don't try to refresh again, return 401",
            ),
            pytest.param(
                ["auth_document_401", "token_grant", "data"],
                [
                    "request_with_token",
                    "token_grant",
                    "request_with_token",
                ],
                True,
                False,
                id="unexpected 401 - refresh and try again",
            ),
        ],
    )
    def test__oauth_get(
        self,
        authenticated_get_fixture: AuthenticatedGetFixture,
        responses: list[str],
        calls: list[str],
        initialized: bool,
        expired: bool,
    ) -> None:
        mock_request_with_timeout = authenticated_get_fixture.request_with_timeout
        authenticated_get = authenticated_get_fixture.authenticated_get()
        if initialized:
            authenticated_get = authenticated_get_fixture.initialize_authenticated_get(
                authenticated_get, expired=expired
            )
        responses_data = [authenticated_get_fixture.responses[r] for r in responses]
        mock_request_with_timeout.side_effect = responses_data
        final_response = responses_data[-1]
        assert (
            authenticated_get._get(
                authenticated_get_fixture.request_url, authenticated_get_fixture.headers
            )
            == final_response
        )
        assert mock_request_with_timeout.call_count == len(calls)
        mock_request_with_timeout.assert_has_calls(
            [authenticated_get_fixture.request_with_timeout_calls[c]() for c in calls]
        )

    def test__oauth_get_allowed_response_codes(
        self, authenticated_get_fixture: AuthenticatedGetFixture
    ) -> None:
        """
        Calling with allowed_response_codes should still allow a token refresh, but if the refresh fails an
        exception will be raised.
        """
        mock_request_with_timeout = authenticated_get_fixture.request_with_timeout
        authenticated_get = authenticated_get_fixture.initialize_authenticated_get()

        mock_request_with_timeout.side_effect = [
            authenticated_get_fixture.responses.get("auth_document_401"),
            authenticated_get_fixture.responses.get("token_grant"),
            authenticated_get_fixture.responses.get("other_401"),
        ]

        with pytest.raises(IntegrationException) as exc_info:
            authenticated_get._get(
                authenticated_get_fixture.request_url,
                authenticated_get_fixture.headers,
                allowed_response_codes=["2xx"],
            )
        assert (
            "Got status code 401 from external server, but can only continue on: 2xx"
            in str(exc_info.value)
        )
        assert mock_request_with_timeout.call_count == 3
        token_grant_call = authenticated_get_fixture.request_with_timeout_calls[
            "token_grant"
        ]()
        request_with_token_call = authenticated_get_fixture.request_with_timeout_calls[
            "request_with_token"
        ](allowed_response_codes=["2xx", 401])

        mock_request_with_timeout.assert_has_calls(
            [
                request_with_token_call,
                token_grant_call,
                request_with_token_call,
            ]
        )
