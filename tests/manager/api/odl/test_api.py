from __future__ import annotations

import datetime
import json
import urllib
import uuid
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import dateutil
import pytest
from freezegun import freeze_time

from palace.manager.api.circulation import (
    DirectFulfillment,
    FetchFulfillment,
    HoldInfo,
    LoanInfo,
    RedirectFulfillment,
)
from palace.manager.api.circulation_exceptions import (
    AlreadyCheckedOut,
    AlreadyOnHold,
    CannotFulfill,
    CannotLoan,
    CannotReturn,
    CurrentlyAvailable,
    HoldOnUnlimitedAccess,
    HoldsNotPermitted,
    NoAvailableCopies,
    NoLicenses,
    NotCheckedOut,
    NotOnHold,
    PatronHoldLimitReached,
    PatronLoanLimitReached,
)
from palace.manager.api.odl.api import OPDS2WithODLApi
from palace.manager.api.odl.constants import FEEDBOOKS_AUDIO
from palace.manager.api.odl.settings import OPDS2AuthType, OPDS2WithODLLibrarySettings
from palace.manager.opds.lcp.status import LoanStatus
from palace.manager.sqlalchemy.constants import MediaTypes
from palace.manager.sqlalchemy.model.licensing import (
    DeliveryMechanism,
    LicensePool,
    LicensePoolDeliveryMechanism,
    RightsStatus,
)
from palace.manager.sqlalchemy.model.patron import Hold, Loan
from palace.manager.sqlalchemy.model.resource import Hyperlink
from palace.manager.sqlalchemy.model.work import Work
from palace.manager.sqlalchemy.util import create
from palace.manager.util.datetime_helpers import datetime_utc, utc_now
from palace.manager.util.http import BadResponseException, RemoteIntegrationException
from tests.fixtures.database import DatabaseTransactionFixture
from tests.fixtures.files import OPDSFilesFixture
from tests.fixtures.odl import OPDS2WithODLApiFixture


class TestOPDS2WithODLApi:
    def test_loan_limit(self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture):
        """Test the loan limit collection setting"""
        # Set the loan limit
        opds2_with_odl_api_fixture.api.loan_limit = 1

        response = opds2_with_odl_api_fixture.checkout(
            patron=opds2_with_odl_api_fixture.patron,
            pool=opds2_with_odl_api_fixture.work.active_license_pool(),
        )
        # Did the loan take place correctly?
        assert (
            response[0].identifier
            == opds2_with_odl_api_fixture.work.presentation_edition.primary_identifier.identifier
        )

        # Second loan for the patron should fail due to the loan limit
        work2: Work = opds2_with_odl_api_fixture.create_work(
            opds2_with_odl_api_fixture.collection
        )
        with pytest.raises(PatronLoanLimitReached) as exc:
            opds2_with_odl_api_fixture.checkout(
                patron=opds2_with_odl_api_fixture.patron,
                pool=work2.active_license_pool(),
            )
        assert exc.value.limit == 1

    @pytest.mark.parametrize(
        "open_access,unlimited_access",
        [
            pytest.param(False, True, id="unlimited_access"),
            pytest.param(True, True, id="open_access"),
        ],
    )
    def test_hold_unlimited_access(
        self,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        open_access: bool,
        unlimited_access: bool,
    ):
        """Tests that placing a hold on an open-access work will always fail,
        since these items are always available to borrow"""
        # Create an open-access work
        pool = opds2_with_odl_api_fixture.work.license_pools[0]
        pool.open_access = open_access
        pool.unlimited_access = unlimited_access

        with pytest.raises(HoldOnUnlimitedAccess):
            opds2_with_odl_api_fixture.api.place_hold(
                opds2_with_odl_api_fixture.patron, "pin", pool, ""
            )

    def test_hold_limit(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ):
        """Test the hold limit collection setting"""
        patron1 = db.patron()

        # First checkout with patron1, then place a hold with the test patron
        pool = opds2_with_odl_api_fixture.work.active_license_pool()
        assert pool is not None
        response = opds2_with_odl_api_fixture.checkout(patron=patron1, pool=pool)
        assert (
            response[0].identifier
            == opds2_with_odl_api_fixture.work.presentation_edition.primary_identifier.identifier
        )

        # Set the hold limit to zero (holds disallowed) and ensure hold fails.
        opds2_with_odl_api_fixture.api.hold_limit = 0
        with pytest.raises(HoldsNotPermitted) as exc:
            opds2_with_odl_api_fixture.api.place_hold(
                opds2_with_odl_api_fixture.patron, "pin", pool, ""
            )
        assert exc.value.problem_detail.title is not None
        assert exc.value.problem_detail.detail is not None
        assert "Holds not permitted" in exc.value.problem_detail.title
        assert "Holds are not permitted" in exc.value.problem_detail.detail

        # Set the hold limit to 1.
        opds2_with_odl_api_fixture.api.hold_limit = 1

        hold_response = opds2_with_odl_api_fixture.api.place_hold(
            opds2_with_odl_api_fixture.patron, "pin", pool, ""
        )
        # Hold was successful
        assert hold_response.hold_position == 1
        create(
            db.session,
            Hold,
            patron_id=opds2_with_odl_api_fixture.patron.id,
            license_pool=pool,
        )

        # Second work should fail for the test patron due to the hold limit
        work2: Work = opds2_with_odl_api_fixture.create_work(
            opds2_with_odl_api_fixture.collection
        )
        # Generate a license
        opds2_with_odl_api_fixture.setup_license(work2)

        # Do the same, patron1 checkout and test patron hold
        pool = work2.active_license_pool()
        assert pool is not None
        response = opds2_with_odl_api_fixture.checkout(patron=patron1, pool=pool)
        assert (
            response[0].identifier
            == work2.presentation_edition.primary_identifier.identifier
        )

        # Hold should fail
        with pytest.raises(PatronHoldLimitReached) as exc2:
            opds2_with_odl_api_fixture.api.place_hold(
                opds2_with_odl_api_fixture.patron, "pin", pool, ""
            )
        assert exc2.value.limit == 1

        # Set the hold limit to None (unlimited) and ensure hold succeeds.
        opds2_with_odl_api_fixture.api.hold_limit = None
        hold_response = opds2_with_odl_api_fixture.api.place_hold(
            opds2_with_odl_api_fixture.patron, "pin", pool, ""
        )
        assert hold_response.hold_position == 1
        create(
            db.session,
            Hold,
            patron_id=opds2_with_odl_api_fixture.patron.id,
            license_pool=pool,
        )
        # Verify that there are now two holds that  our test patron has both of them.
        assert 2 == db.session.query(Hold).count()
        assert (
            2
            == db.session.query(Hold)
            .filter(Hold.patron_id == opds2_with_odl_api_fixture.patron.id)
            .count()
        )

    @pytest.mark.parametrize(
        "status_code",
        [pytest.param(200, id="existing loan"), pytest.param(200, id="new loan")],
    )
    def test__request_loan_status_success(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture, status_code: int
    ) -> None:
        expected_document = opds2_with_odl_api_fixture.loan_status_document("active")

        opds2_with_odl_api_fixture.mock_http.queue_response(
            status_code, content=expected_document.model_dump_json()
        )
        requested_document = opds2_with_odl_api_fixture.api._request_loan_status(
            "GET", "http://loan"
        )
        assert "GET" == opds2_with_odl_api_fixture.mock_http.requests_methods.pop()
        assert "http://loan" == opds2_with_odl_api_fixture.mock_http.requests.pop()
        assert requested_document == expected_document

    @pytest.mark.parametrize(
        "status, headers, content, exception, expected_log_message",
        [
            pytest.param(
                200,
                {},
                "not json",
                RemoteIntegrationException,
                "Error validating Loan Status Document. 'http://loan' returned and invalid document.",
                id="invalid json",
            ),
            pytest.param(
                200,
                {},
                json.dumps(dict(status="unknown")),
                RemoteIntegrationException,
                "Error validating Loan Status Document. 'http://loan' returned and invalid document.",
                id="invalid document",
            ),
            pytest.param(
                403,
                {"header": "value"},
                "server error",
                RemoteIntegrationException,
                "Error requesting Loan Status Document. 'http://loan' returned status code 403. "
                "Response headers: header: value. Response content: server error.",
                id="bad status code",
            ),
            pytest.param(
                403,
                {"Content-Type": "application/api-problem+json"},
                json.dumps(
                    dict(
                        type="http://problem-detail-uri",
                        title="server error",
                        detail="broken",
                    )
                ),
                RemoteIntegrationException,
                "Error requesting Loan Status Document. 'http://loan' returned status code 403. "
                "Problem Detail: 'http://problem-detail-uri' - server error - broken",
                id="problem detail response",
            ),
        ],
    )
    def test__request_loan_status_errors(
        self,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        caplog: pytest.LogCaptureFixture,
        status: int,
        headers: dict[str, str],
        content: str,
        exception: type[Exception],
        expected_log_message: str,
    ) -> None:
        # The response can't be parsed as JSON.
        opds2_with_odl_api_fixture.mock_http.queue_response(
            status, other_headers=headers, content=content
        )
        with pytest.raises(exception):
            opds2_with_odl_api_fixture.api._request_loan_status("GET", "http://loan")
        assert expected_log_message in caplog.text

    def test_checkin_success(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # A patron has a copy of this book checked out.
        opds2_with_odl_api_fixture.setup_license(concurrency=7, available=6)

        loan, _ = opds2_with_odl_api_fixture.license.loan_to(
            opds2_with_odl_api_fixture.patron
        )
        loan.external_identifier = "http://loan/" + db.fresh_str()
        loan.end = utc_now() + datetime.timedelta(days=3)

        # The patron returns the book successfully.
        opds2_with_odl_api_fixture.checkin()
        assert 2 == len(opds2_with_odl_api_fixture.mock_http.requests)
        assert "http://loan" in opds2_with_odl_api_fixture.mock_http.requests[0]
        assert "http://return" == opds2_with_odl_api_fixture.mock_http.requests[1]

        # The pool's availability has increased
        assert 7 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == db.session.query(Loan).count()

        # The license on the pool has also been updated
        assert 7 == opds2_with_odl_api_fixture.license.checkouts_available

    def test_checkin_success_with_holds_queue(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # A patron has the only copy of this book checked out.
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=0)
        loan, _ = opds2_with_odl_api_fixture.license.loan_to(
            opds2_with_odl_api_fixture.patron
        )
        loan.external_identifier = "http://loan/" + db.fresh_str()
        loan.end = utc_now() + datetime.timedelta(days=3)

        # Another patron has the book on hold.
        patron_with_hold = db.patron()
        opds2_with_odl_api_fixture.pool.patrons_in_hold_queue = 1
        hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            patron_with_hold, start=utc_now(), end=None, position=1
        )

        # The first patron returns the book successfully.
        opds2_with_odl_api_fixture.checkin()
        assert 2 == len(opds2_with_odl_api_fixture.mock_http.requests)
        assert "http://loan" in opds2_with_odl_api_fixture.mock_http.requests[0]
        assert "http://return" == opds2_with_odl_api_fixture.mock_http.requests[1]

        # Now the license is reserved for the next patron.
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 1 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 0 == db.session.query(Loan).count()
        assert 0 == hold.position

    def test_checkin_not_checked_out(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # Not checked out locally.
        pytest.raises(
            NotCheckedOut,
            opds2_with_odl_api_fixture.api.checkin,
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
        )

        # Not checked out according to the distributor.
        loan, _ = opds2_with_odl_api_fixture.license.loan_to(
            opds2_with_odl_api_fixture.patron
        )
        loan.external_identifier = db.fresh_str()
        loan.end = utc_now() + datetime.timedelta(days=3)

        opds2_with_odl_api_fixture.mock_http.queue_response(
            200,
            content=opds2_with_odl_api_fixture.loan_status_document(
                "revoked"
            ).model_dump_json(),
        )
        # Checking in silently does nothing.
        opds2_with_odl_api_fixture.api.checkin(
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
        )

    def test_checkin_cannot_return(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # Not fulfilled yet, but no return link from the distributor.
        loan, ignore = opds2_with_odl_api_fixture.license.loan_to(
            opds2_with_odl_api_fixture.patron
        )
        loan.external_identifier = db.fresh_str()
        loan.end = utc_now() + datetime.timedelta(days=3)

        opds2_with_odl_api_fixture.mock_http.queue_response(
            200,
            content=opds2_with_odl_api_fixture.loan_status_document(
                "ready", return_link=False
            ).model_dump_json(),
        )
        # Checking in raises the CannotReturn exception, since the distributor
        # does not support returning the book.
        with pytest.raises(CannotReturn):
            opds2_with_odl_api_fixture.api.checkin(
                opds2_with_odl_api_fixture.patron,
                "pin",
                opds2_with_odl_api_fixture.pool,
            )

        # If the return link doesn't change the status, we raise the same exception.
        lsd = opds2_with_odl_api_fixture.loan_status_document(
            "ready", return_link="http://return"
        ).model_dump_json()

        opds2_with_odl_api_fixture.mock_http.queue_response(200, content=lsd)
        opds2_with_odl_api_fixture.mock_http.queue_response(200, content=lsd)
        with pytest.raises(CannotReturn):
            opds2_with_odl_api_fixture.api.checkin(
                opds2_with_odl_api_fixture.patron,
                "pin",
                opds2_with_odl_api_fixture.pool,
            )

    def test_checkin_open_access(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # Checking in an open-access book doesn't need to call out to the distributor API.
        oa_work = db.work(
            with_open_access_download=True,
            collection=opds2_with_odl_api_fixture.collection,
        )
        pool = oa_work.license_pools[0]
        loan, ignore = pool.loan_to(opds2_with_odl_api_fixture.patron)

        # make sure that _checkin isn't called since it is not needed for an open access work
        opds2_with_odl_api_fixture.api._checkin = MagicMock(
            side_effect=Exception("Should not be called")
        )

        opds2_with_odl_api_fixture.api.checkin(
            opds2_with_odl_api_fixture.patron, "pin", pool
        )

    def test_checkout_success(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # This book is available to check out.
        opds2_with_odl_api_fixture.setup_license(concurrency=6, available=6, left=30)

        # A patron checks out the book successfully.
        loan_url = db.fresh_str()
        loan, _ = opds2_with_odl_api_fixture.checkout(loan_url=loan_url)

        assert opds2_with_odl_api_fixture.collection == loan.collection(db.session)
        assert opds2_with_odl_api_fixture.pool.data_source.name == loan.data_source_name
        assert opds2_with_odl_api_fixture.pool.identifier.type == loan.identifier_type
        assert opds2_with_odl_api_fixture.pool.identifier.identifier == loan.identifier
        assert loan.start_date is not None
        assert loan.start_date > utc_now() - datetime.timedelta(minutes=1)
        assert loan.start_date < utc_now() + datetime.timedelta(minutes=1)
        assert datetime_utc(3017, 10, 21, 11, 12, 13) == loan.end_date
        assert loan_url == loan.external_identifier
        assert 1 == db.session.query(Loan).count()

        # Now the patron has a loan in the database that matches the LoanInfo
        # returned by the API.
        db_loan = db.session.query(Loan).one()
        assert opds2_with_odl_api_fixture.pool == db_loan.license_pool
        assert opds2_with_odl_api_fixture.license == db_loan.license
        assert loan.start_date == db_loan.start
        assert loan.end_date == db_loan.end

        # The pool's availability and the license's remaining checkouts have decreased.
        assert 5 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 29 == opds2_with_odl_api_fixture.license.checkouts_left

        # The parameters that we templated into the checkout URL are correct.
        requested_url = opds2_with_odl_api_fixture.mock_http.requests.pop()

        parsed = urlparse(requested_url)
        assert "https" == parsed.scheme
        assert "loan.feedbooks.net" == parsed.netloc
        params = parse_qs(parsed.query)

        assert (
            opds2_with_odl_api_fixture.api.settings.passphrase_hint == params["hint"][0]
        )
        assert (
            opds2_with_odl_api_fixture.api.settings.passphrase_hint_url
            == params["hint_url"][0]
        )

        assert opds2_with_odl_api_fixture.license.identifier == params["id"][0]

        # The checkout id and patron id are random UUIDs.
        checkout_id = params["checkout_id"][0]
        assert uuid.UUID(checkout_id)
        patron_id = params["patron_id"][0]
        assert uuid.UUID(patron_id)

        # Loans expire in 21 days by default.
        now = utc_now()
        after_expiration = now + datetime.timedelta(days=23)
        expires = urllib.parse.unquote(params["expires"][0])

        # The expiration time passed to the server is associated with
        # the UTC time zone.
        assert expires.endswith("+00:00")
        expires_t = dateutil.parser.parse(expires)
        assert expires_t.tzinfo == dateutil.tz.tz.tzutc()

        # It's a time in the future, but not _too far_ in the future.
        assert expires_t > now
        assert expires_t < after_expiration

        notification_url = urllib.parse.unquote_plus(params["notification_url"][0])
        assert (
            "http://odl_notify?library_short_name=%s&loan_id=%s"
            % (opds2_with_odl_api_fixture.library.short_name, db_loan.id)
            == notification_url
        )

    def test_checkout_open_access(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # This book is available to check out.
        oa_work = db.work(
            with_open_access_download=True,
            collection=opds2_with_odl_api_fixture.collection,
        )
        loan = opds2_with_odl_api_fixture.api_checkout(
            licensepool=oa_work.license_pools[0],
        )

        assert loan.collection(db.session) == opds2_with_odl_api_fixture.collection
        assert loan.identifier == oa_work.license_pools[0].identifier.identifier
        assert loan.identifier_type == oa_work.license_pools[0].identifier.type
        assert loan.start_date is None
        assert loan.end_date is None
        assert loan.external_identifier is None

    def test_checkout_success_with_hold(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # A patron has this book on hold, and the book just became available to check out.
        opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron,
            start=utc_now() - datetime.timedelta(days=1),
            position=0,
        )
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=1, left=5)

        assert opds2_with_odl_api_fixture.pool.licenses_available == 0
        assert opds2_with_odl_api_fixture.pool.licenses_reserved == 1
        assert opds2_with_odl_api_fixture.pool.patrons_in_hold_queue == 1

        # The patron checks out the book.
        loan_url = db.fresh_str()
        loan, _ = opds2_with_odl_api_fixture.checkout(loan_url=loan_url)

        # The patron gets a loan successfully.
        assert opds2_with_odl_api_fixture.collection == loan.collection(db.session)
        assert opds2_with_odl_api_fixture.pool.data_source.name == loan.data_source_name
        assert opds2_with_odl_api_fixture.pool.identifier.type == loan.identifier_type
        assert opds2_with_odl_api_fixture.pool.identifier.identifier == loan.identifier
        assert loan.start_date is not None
        assert loan.start_date > utc_now() - datetime.timedelta(minutes=1)
        assert loan.start_date < utc_now() + datetime.timedelta(minutes=1)
        assert datetime_utc(3017, 10, 21, 11, 12, 13) == loan.end_date
        assert loan_url == loan.external_identifier
        assert 1 == db.session.query(Loan).count()

        db_loan = db.session.query(Loan).one()
        assert opds2_with_odl_api_fixture.pool == db_loan.license_pool
        assert opds2_with_odl_api_fixture.license == db_loan.license
        assert 4 == opds2_with_odl_api_fixture.license.checkouts_left

        # The book is no longer reserved for the patron, and the hold has been deleted.
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 0 == db.session.query(Hold).count()

    def test_checkout_success_external_identifier_fallback(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        opds_files_fixture: OPDSFilesFixture,
    ) -> None:
        # This book is available to check out.
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=1)

        # The server returns a loan status document with no self link, but the license document
        # has a link to the loan status document, so we make the extra request to get the external identifier
        # from the license document.
        opds2_with_odl_api_fixture.mock_http.queue_response(
            201,
            content=opds2_with_odl_api_fixture.loan_status_document(
                self_link=False,
            ).model_dump_json(),
        )
        opds2_with_odl_api_fixture.mock_http.queue_response(
            201, content=opds_files_fixture.sample_text("lcp/license/ul.json")
        )
        loan = opds2_with_odl_api_fixture.api_checkout()
        assert (
            loan.external_identifier
            == "https://license.example.com/licenses/123-456/status"
        )
        assert len(opds2_with_odl_api_fixture.mock_http.requests) == 2

    def test_checkout_already_checked_out(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        opds2_with_odl_api_fixture.setup_license(concurrency=2, available=1)

        # Checkout succeeds the first time
        opds2_with_odl_api_fixture.checkout()

        # But raises an exception the second time
        pytest.raises(AlreadyCheckedOut, opds2_with_odl_api_fixture.checkout)

        assert 1 == db.session.query(Loan).count()

    def test_checkout_expired_hold(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # The patron was at the beginning of the hold queue, but the hold already expired.
        yesterday = utc_now() - datetime.timedelta(days=1)
        hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron,
            start=yesterday,
            end=yesterday,
            position=0,
        )
        other_hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), start=utc_now()
        )
        opds2_with_odl_api_fixture.setup_license(concurrency=2, available=1)

        with pytest.raises(NoAvailableCopies):
            opds2_with_odl_api_fixture.api_checkout()

    def test_checkout_no_available_copies(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # A different patron has the only copy checked out.
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=0)
        existing_loan, _ = opds2_with_odl_api_fixture.license.loan_to(db.patron())

        with pytest.raises(NoAvailableCopies):
            opds2_with_odl_api_fixture.api_checkout()

        assert 1 == db.session.query(Loan).count()

        db.session.delete(existing_loan)

        now = utc_now()
        yesterday = now - datetime.timedelta(days=1)
        last_week = now - datetime.timedelta(weeks=1)

        # A different patron has the only copy reserved.
        other_patron_hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), position=0, start=last_week
        )
        opds2_with_odl_api_fixture.pool.update_availability_from_licenses()

        with pytest.raises(NoAvailableCopies):
            opds2_with_odl_api_fixture.api_checkout()

        assert 0 == db.session.query(Loan).count()

        # The patron has a hold, but another patron is ahead in the holds queue.
        hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), position=1, start=yesterday
        )
        opds2_with_odl_api_fixture.pool.update_availability_from_licenses()

        with pytest.raises(NoAvailableCopies):
            opds2_with_odl_api_fixture.api_checkout()

        assert 0 == db.session.query(Loan).count()

        # The patron has the first hold, but it's expired.
        hold.start = last_week - datetime.timedelta(days=1)
        hold.end = yesterday
        opds2_with_odl_api_fixture.pool.update_availability_from_licenses()

        with pytest.raises(NoAvailableCopies):
            opds2_with_odl_api_fixture.api_checkout()

        assert 0 == db.session.query(Loan).count()

    @pytest.mark.parametrize(
        "response_type",
        ["application/api-problem+json", "application/problem+json"],
    )
    def test_checkout_no_available_copies_unknown_to_us(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        response_type: str,
    ) -> None:
        """
        The title has no available copies, but we are out of sync with the distributor, so we think there
        are copies available.
        """
        # We think there are copies available.
        license = opds2_with_odl_api_fixture.setup_license(concurrency=1, available=1)

        # But the distributor says there are no available copies.
        opds2_with_odl_api_fixture.mock_http.queue_response(
            400,
            response_type,
            content=opds2_with_odl_api_fixture.files.sample_text("unavailable.json"),
        )

        with pytest.raises(NoAvailableCopies):
            opds2_with_odl_api_fixture.api_checkout()

        assert db.session.query(Loan).count() == 0
        assert license.license_pool.licenses_available == 0
        assert license.checkouts_available == 0

    def test_checkout_failures(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # We think there are copies available.
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=1)

        # Test the case where we get bad JSON back from the distributor.
        opds2_with_odl_api_fixture.mock_http.queue_response(
            400,
            "application/api-problem+json",
            content="hot garbage",
        )

        with pytest.raises(BadResponseException):
            opds2_with_odl_api_fixture.api_checkout()

        # Test the case where we just get an unknown bad response.
        opds2_with_odl_api_fixture.mock_http.queue_response(
            500, "text/plain", content="halt and catch fire 🔥"
        )
        with pytest.raises(BadResponseException):
            opds2_with_odl_api_fixture.api_checkout()

    def test_checkout_no_licenses(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=1, left=0)

        with pytest.raises(NoLicenses):
            opds2_with_odl_api_fixture.api_checkout()

        assert 0 == db.session.query(Loan).count()

    def test_checkout_when_all_licenses_expired(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture
    ) -> None:
        # license expired by expiration date
        opds2_with_odl_api_fixture.setup_license(
            concurrency=1,
            available=2,
            left=1,
            expires=utc_now() - datetime.timedelta(weeks=1),
        )

        with pytest.raises(NoLicenses):
            opds2_with_odl_api_fixture.api_checkout()

        # license expired by no remaining checkouts
        opds2_with_odl_api_fixture.setup_license(
            concurrency=1,
            available=2,
            left=0,
            expires=utc_now() + datetime.timedelta(weeks=1),
        )

        with pytest.raises(NoLicenses):
            opds2_with_odl_api_fixture.api_checkout()

    def test_checkout_cannot_loan(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        opds2_with_odl_api_fixture.mock_http.queue_response(
            200,
            content=opds2_with_odl_api_fixture.loan_status_document(
                "revoked"
            ).model_dump_json(),
        )
        with pytest.raises(CannotLoan):
            opds2_with_odl_api_fixture.api_checkout()
        assert 0 == db.session.query(Loan).count()

        # No external identifier.
        opds2_with_odl_api_fixture.mock_http.queue_response(
            200,
            content=opds2_with_odl_api_fixture.loan_status_document(
                self_link=False, license_link=False
            ).model_dump_json(),
        )
        with pytest.raises(CannotLoan):
            opds2_with_odl_api_fixture.api_checkout()
        assert 0 == db.session.query(Loan).count()

    @pytest.mark.parametrize(
        "drm_scheme, correct_type, correct_link, links",
        [
            pytest.param(
                DeliveryMechanism.ADOBE_DRM,
                DeliveryMechanism.ADOBE_DRM,
                "http://acsm",
                [
                    {
                        "rel": "license",
                        "href": "http://acsm",
                        "type": DeliveryMechanism.ADOBE_DRM,
                    }
                ],
                id="adobe drm",
            ),
            pytest.param(
                DeliveryMechanism.LCP_DRM,
                DeliveryMechanism.LCP_DRM,
                "http://lcp",
                [
                    {
                        "rel": "license",
                        "href": "http://lcp",
                        "type": DeliveryMechanism.LCP_DRM,
                    }
                ],
                id="lcp drm",
            ),
            pytest.param(
                DeliveryMechanism.NO_DRM,
                "application/epub+zip",
                "http://publication",
                [
                    {
                        "rel": "publication",
                        "href": "http://publication",
                        "type": "application/epub+zip",
                    }
                ],
                id="no drm",
            ),
            pytest.param(
                DeliveryMechanism.FEEDBOOKS_AUDIOBOOK_DRM,
                FEEDBOOKS_AUDIO,
                "http://correct",
                [
                    {
                        "rel": "license",
                        "href": "http://acsm",
                        "type": DeliveryMechanism.ADOBE_DRM,
                    },
                    {
                        "rel": "manifest",
                        "href": "http://correct",
                        "type": FEEDBOOKS_AUDIO,
                    },
                ],
                id="feedbooks audio",
            ),
        ],
    )
    def test_fulfill_success(
        self,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        db: DatabaseTransactionFixture,
        drm_scheme: str,
        correct_type: str,
        correct_link: str,
        links: list[dict[str, str]],
    ) -> None:
        # Fulfill a loan in a way that gives access to a license file.
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=1)
        opds2_with_odl_api_fixture.checkout()

        lpdm = MagicMock(spec=LicensePoolDeliveryMechanism)
        lpdm.delivery_mechanism = MagicMock(spec=DeliveryMechanism)
        lpdm.delivery_mechanism.content_type = (
            "ignored/format" if drm_scheme != DeliveryMechanism.NO_DRM else correct_type
        )
        lpdm.delivery_mechanism.drm_scheme = drm_scheme

        lsd = opds2_with_odl_api_fixture.loan_status_document("active", links=links)
        opds2_with_odl_api_fixture.mock_http.queue_response(
            200, content=lsd.model_dump_json()
        )
        fulfillment = opds2_with_odl_api_fixture.api.fulfill(
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
            lpdm,
        )
        assert (
            isinstance(fulfillment, FetchFulfillment)
            if drm_scheme != DeliveryMechanism.NO_DRM
            else isinstance(fulfillment, RedirectFulfillment)
        )
        assert correct_link == fulfillment.content_link  # type: ignore[attr-defined]
        assert correct_type == fulfillment.content_type  # type: ignore[attr-defined]
        if isinstance(fulfillment, FetchFulfillment):
            assert fulfillment.allowed_response_codes == ["2xx"]

    def test_fulfill_open_access(
        self,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        db: DatabaseTransactionFixture,
    ) -> None:
        oa_work = db.work(
            with_open_access_download=True,
            collection=opds2_with_odl_api_fixture.collection,
        )
        pool = oa_work.license_pools[0]
        loan, ignore = pool.loan_to(opds2_with_odl_api_fixture.patron)

        # If we can't find a delivery mechanism, we can't fulfill the loan.
        mock_lpdm = MagicMock(
            spec=LicensePoolDeliveryMechanism,
            delivery_mechanism=MagicMock(drm_scheme=None),
        )
        with pytest.raises(CannotFulfill):
            opds2_with_odl_api_fixture.api.fulfill(
                opds2_with_odl_api_fixture.patron, "pin", pool, mock_lpdm
            )

        lpdm = pool.delivery_mechanisms[0]
        fulfillment = opds2_with_odl_api_fixture.api.fulfill(
            opds2_with_odl_api_fixture.patron, "pin", pool, lpdm
        )

        assert isinstance(fulfillment, RedirectFulfillment)
        assert fulfillment.content_link == pool.open_access_download_url
        assert fulfillment.content_type == lpdm.delivery_mechanism.content_type

    @freeze_time()
    def test_fulfill_bearer_token(
        self,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        db: DatabaseTransactionFixture,
    ) -> None:
        opds2_with_odl_api_fixture.api.mock_auth_type = OPDS2AuthType.OAUTH
        work = db.work()
        pool = db.licensepool(
            work.presentation_edition,
            work=work,
            unlimited_access=True,
            with_open_access_download=True,
            collection=opds2_with_odl_api_fixture.collection,
        )
        url = "http://test.com/" + db.fresh_str()
        media_type = MediaTypes.EPUB_MEDIA_TYPE
        link, new = pool.identifier.add_link(
            Hyperlink.GENERIC_OPDS_ACQUISITION, url, pool.data_source, media_type
        )

        # Add a DeliveryMechanism for this download
        lpdm = pool.set_delivery_mechanism(
            media_type,
            DeliveryMechanism.BEARER_TOKEN,
            RightsStatus.IN_COPYRIGHT,
            link.resource,
        )

        pool.loan_to(opds2_with_odl_api_fixture.patron)
        fulfillment = opds2_with_odl_api_fixture.api.fulfill(
            opds2_with_odl_api_fixture.patron, "pin", pool, lpdm
        )

        assert isinstance(fulfillment, DirectFulfillment)
        assert fulfillment.content_type == DeliveryMechanism.BEARER_TOKEN
        assert fulfillment.content is not None
        token_doc = json.loads(fulfillment.content)
        assert opds2_with_odl_api_fixture.api._session_token is not None
        assert (
            token_doc.get("access_token")
            == opds2_with_odl_api_fixture.api._session_token.token
        )
        assert token_doc.get("expires_in") == int(
            (
                opds2_with_odl_api_fixture.api._session_token.expires - utc_now()
            ).total_seconds()
        )
        assert token_doc.get("token_type") == "Bearer"
        assert token_doc.get("location") == url
        assert opds2_with_odl_api_fixture.api.refresh_token_calls == 1

        # A second call to fulfill should not refresh the token
        fulfillment_2 = opds2_with_odl_api_fixture.api.fulfill(
            opds2_with_odl_api_fixture.patron, "pin", pool, lpdm
        )
        assert isinstance(fulfillment_2, DirectFulfillment)
        assert fulfillment_2.content == fulfillment.content
        assert opds2_with_odl_api_fixture.api.refresh_token_calls == 1

    @pytest.mark.parametrize(
        "status_document, updated_availability",
        [
            pytest.param(
                OPDS2WithODLApiFixture.loan_status_document("revoked"),
                True,
                id="revoked",
            ),
            pytest.param(
                OPDS2WithODLApiFixture.loan_status_document("cancelled"),
                True,
                id="cancelled",
            ),
            pytest.param(
                OPDS2WithODLApiFixture.loan_status_document("active"),
                False,
                id="missing link",
            ),
        ],
    )
    def test_fulfill_cannot_fulfill(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
        status_document: LoanStatus,
        updated_availability: bool,
    ) -> None:
        opds2_with_odl_api_fixture.setup_license(concurrency=7, available=7)
        opds2_with_odl_api_fixture.checkout()

        assert 1 == db.session.query(Loan).count()
        assert 6 == opds2_with_odl_api_fixture.pool.licenses_available

        opds2_with_odl_api_fixture.mock_http.queue_response(
            200, content=status_document.model_dump_json()
        )
        with pytest.raises(CannotFulfill):
            opds2_with_odl_api_fixture.api.fulfill(
                opds2_with_odl_api_fixture.patron,
                "pin",
                opds2_with_odl_api_fixture.pool,
                MagicMock(),
            )

        if updated_availability:
            # The pool's availability has been updated and the local
            # loan has been deleted, since we found out the loan is
            # no longer active.
            assert 7 == opds2_with_odl_api_fixture.pool.licenses_available
            assert 0 == db.session.query(Loan).count()

    def _holdinfo_from_hold(self, hold: Hold) -> HoldInfo:
        pool: LicensePool = hold.license_pool
        return HoldInfo(
            pool.collection,
            pool.data_source.name,
            pool.identifier.type,
            pool.identifier.identifier,
            hold.start,
            hold.end,
            hold.position,
        )

    def test_count_holds_before(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        now = utc_now()
        yesterday = now - datetime.timedelta(days=1)
        tomorrow = now + datetime.timedelta(days=1)
        last_week = now - datetime.timedelta(weeks=1)

        hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, start=now
        )

        info = self._holdinfo_from_hold(hold)
        assert 0 == opds2_with_odl_api_fixture.api._count_holds_before(
            info, hold.license_pool
        )

        # A previous hold.
        opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        assert 1 == opds2_with_odl_api_fixture.api._count_holds_before(
            info, hold.license_pool
        )

        # Expired holds don't count.
        opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), start=last_week, end=yesterday, position=0
        )
        assert 1 == opds2_with_odl_api_fixture.api._count_holds_before(
            info, hold.license_pool
        )

        # Later holds don't count.
        opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=tomorrow)
        assert 1 == opds2_with_odl_api_fixture.api._count_holds_before(
            info, hold.license_pool
        )

        # Holds on another pool don't count.
        other_pool = db.licensepool(None)
        other_pool.on_hold_to(opds2_with_odl_api_fixture.patron, start=yesterday)
        assert 1 == opds2_with_odl_api_fixture.api._count_holds_before(
            info, hold.license_pool
        )

        for i in range(3):
            opds2_with_odl_api_fixture.pool.on_hold_to(
                db.patron(), start=yesterday, end=tomorrow, position=1
            )
        assert 4 == opds2_with_odl_api_fixture.api._count_holds_before(
            info, hold.license_pool
        )

    def test_update_hold_end_date(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        now = utc_now()
        tomorrow = now + datetime.timedelta(days=1)
        yesterday = now - datetime.timedelta(days=1)
        next_week = now + datetime.timedelta(days=7)
        last_week = now - datetime.timedelta(days=7)

        opds2_with_odl_api_fixture.pool.licenses_owned = 1
        opds2_with_odl_api_fixture.pool.licenses_reserved = 1

        hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, start=now, position=0
        )
        info = self._holdinfo_from_hold(hold)
        library = hold.patron.library

        # Set the reservation period and loan period.
        db.integration_library_configuration(
            opds2_with_odl_api_fixture.collection.integration_configuration,
            library=library,
            settings=OPDS2WithODLLibrarySettings(
                default_reservation_period=3, ebook_loan_duration=6
            ),
        )

        # A hold that's already reserved and has an end date doesn't change.
        info.end_date = tomorrow
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert tomorrow == info.end_date
        info.end_date = yesterday
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert yesterday == info.end_date

        # Updating a hold that's reserved but doesn't have an end date starts the
        # reservation period.
        info.end_date = None
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert info.end_date is not None
        assert info.end_date < next_week  # type: ignore[unreachable]
        assert info.end_date > now

        # Updating a hold that has an end date but just became reserved starts
        # the reservation period.
        info.end_date = yesterday
        info.hold_position = 1
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert info.end_date < next_week
        assert info.end_date > now

        # When there's a holds queue, the end date is the maximum time it could take for
        # a license to become available.

        # One copy, one loan, hold position 1.
        # The hold will be available as soon as the loan expires.
        opds2_with_odl_api_fixture.pool.licenses_available = 0
        opds2_with_odl_api_fixture.pool.licenses_reserved = 0
        opds2_with_odl_api_fixture.pool.licenses_owned = 1
        loan, ignore = opds2_with_odl_api_fixture.license.loan_to(
            db.patron(), end=tomorrow
        )
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert tomorrow == info.end_date

        # One copy, one loan, hold position 2.
        # The hold will be available after the loan expires + 1 cycle.
        first_hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), start=last_week
        )
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert tomorrow + datetime.timedelta(days=9) == info.end_date

        # Two copies, one loan, one reserved hold, hold position 2.
        # The hold will be available after the loan expires.
        opds2_with_odl_api_fixture.pool.licenses_reserved = 1
        opds2_with_odl_api_fixture.pool.licenses_owned = 2
        opds2_with_odl_api_fixture.license.checkouts_available = 2
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert tomorrow == info.end_date

        # Two copies, one loan, one reserved hold, hold position 3.
        # The hold will be available after the reserved hold is checked out
        # at the latest possible time and that loan expires.
        second_hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), start=yesterday
        )
        first_hold.end = next_week
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=6) == info.end_date

        # One copy, no loans, one reserved hold, hold position 3.
        # The hold will be available after the reserved hold is checked out
        # at the latest possible time and that loan expires + 1 cycle.
        db.session.delete(loan)
        opds2_with_odl_api_fixture.pool.licenses_owned = 1
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=15) == info.end_date

        # One copy, no loans, one reserved hold, hold position 2.
        # The hold will be available after the reserved hold is checked out
        # at the latest possible time and that loan expires.
        db.session.delete(second_hold)
        opds2_with_odl_api_fixture.pool.licenses_owned = 1
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=6) == info.end_date

        db.session.delete(first_hold)

        # Ten copies, seven loans, three reserved holds, hold position 9.
        # The hold will be available after the sixth loan expires.
        opds2_with_odl_api_fixture.pool.licenses_owned = 10
        for i in range(5):
            opds2_with_odl_api_fixture.pool.loan_to(db.patron(), end=next_week)
        opds2_with_odl_api_fixture.pool.loan_to(
            db.patron(), end=next_week + datetime.timedelta(days=1)
        )
        opds2_with_odl_api_fixture.pool.loan_to(
            db.patron(), end=next_week + datetime.timedelta(days=2)
        )
        opds2_with_odl_api_fixture.pool.licenses_reserved = 3
        for i in range(3):
            opds2_with_odl_api_fixture.pool.on_hold_to(
                db.patron(),
                start=last_week + datetime.timedelta(days=i),
                end=next_week + datetime.timedelta(days=i),
                position=0,
            )
        for i in range(5):
            opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=1) == info.end_date

        # Ten copies, seven loans, three reserved holds, hold position 12.
        # The hold will be available after the second reserved hold is checked
        # out and that loan expires.
        for i in range(3):
            opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=7) == info.end_date

        # Ten copies, seven loans, three reserved holds, hold position 29.
        # The hold will be available after the sixth loan expires + 2 cycles.
        for i in range(17):
            opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=19) == info.end_date

        # Ten copies, seven loans, three reserved holds, hold position 32.
        # The hold will be available after the second reserved hold is checked
        # out and that loan expires + 2 cycles.
        for i in range(3):
            opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        opds2_with_odl_api_fixture.api._update_hold_end_date(
            info, hold.license_pool, library=library
        )
        assert next_week + datetime.timedelta(days=25) == info.end_date

    def test_update_hold_position(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        now = utc_now()
        yesterday = now - datetime.timedelta(days=1)
        tomorrow = now + datetime.timedelta(days=1)

        hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, start=now
        )
        info = self._holdinfo_from_hold(hold)

        opds2_with_odl_api_fixture.pool.licenses_owned = 1

        # When there are no other holds and no licenses reserved, hold position is 1.
        loan, _ = opds2_with_odl_api_fixture.license.loan_to(db.patron())
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 1 == info.hold_position

        # When a license is reserved, position is 0.
        db.session.delete(loan)
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 0 == info.hold_position

        # If another hold has the reserved licenses, position is 2.
        opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 2 == info.hold_position

        # If another license is reserved, position goes back to 0.
        opds2_with_odl_api_fixture.pool.licenses_owned = 2
        opds2_with_odl_api_fixture.license.checkouts_available = 2
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 0 == info.hold_position

        # If there's an earlier hold but it expired, it doesn't
        # affect the position.
        opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), start=yesterday, end=yesterday, position=0
        )
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 0 == info.hold_position

        # Hold position is after all earlier non-expired holds...
        for i in range(3):
            opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=yesterday)
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 5 == info.hold_position

        # and before any later holds.
        for i in range(2):
            opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), start=tomorrow)
        opds2_with_odl_api_fixture.api._update_hold_position(info, hold.license_pool)
        assert 5 == info.hold_position

    def test_update_hold_data(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        hold, is_new = opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron,
            utc_now(),
            utc_now() + datetime.timedelta(days=100),
            9,
        )
        opds2_with_odl_api_fixture.api._update_hold_data(hold)
        assert hold.position == 0
        assert hold.end.date() == (hold.start + datetime.timedelta(days=3)).date()

    def test_update_hold_queue(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        licenses = [opds2_with_odl_api_fixture.license]

        # If there's no holds queue when we try to update the queue, it
        # will remove a reserved license and make it available instead.
        opds2_with_odl_api_fixture.pool.licenses_owned = 1
        opds2_with_odl_api_fixture.pool.licenses_available = 0
        opds2_with_odl_api_fixture.pool.licenses_reserved = 1
        opds2_with_odl_api_fixture.pool.patrons_in_hold_queue = 0
        last_update = utc_now() - datetime.timedelta(minutes=5)
        opds2_with_odl_api_fixture.work.last_update_time = last_update
        opds2_with_odl_api_fixture.api.update_licensepool(
            opds2_with_odl_api_fixture.pool
        )
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        # The work's last update time is changed so it will be moved up in the crawlable OPDS feed.
        assert opds2_with_odl_api_fixture.work.last_update_time > last_update

        # If there are holds, a license will get reserved for the next hold
        # and its end date will be set.
        hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, start=utc_now(), position=1
        )
        later_hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), start=utc_now() + datetime.timedelta(days=1), position=2
        )
        opds2_with_odl_api_fixture.api.update_licensepool(
            opds2_with_odl_api_fixture.pool
        )

        # The pool's licenses were updated.
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 2 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue

        # And the first hold changed.
        assert 0 == hold.position
        assert hold.end - utc_now() - datetime.timedelta(days=3) < datetime.timedelta(
            hours=1
        )

        # The later hold is the same.
        assert 2 == later_hold.position

        # Now there's a reserved hold. If we add another license, it's reserved and,
        # the later hold is also updated.
        l = db.license(
            opds2_with_odl_api_fixture.pool, terms_concurrency=1, checkouts_available=1
        )
        licenses.append(l)
        opds2_with_odl_api_fixture.api.update_licensepool(
            opds2_with_odl_api_fixture.pool
        )

        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 2 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 2 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 0 == later_hold.position
        assert later_hold.end - utc_now() - datetime.timedelta(
            days=3
        ) < datetime.timedelta(hours=1)

        # Now there are no more holds. If we add another license,
        # it ends up being available.
        l = db.license(
            opds2_with_odl_api_fixture.pool, terms_concurrency=1, checkouts_available=1
        )
        licenses.append(l)
        opds2_with_odl_api_fixture.api.update_licensepool(
            opds2_with_odl_api_fixture.pool
        )
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 2 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 2 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue

        # License pool is updated when the holds are removed.
        db.session.delete(hold)
        db.session.delete(later_hold)
        opds2_with_odl_api_fixture.api.update_licensepool(
            opds2_with_odl_api_fixture.pool
        )
        assert 3 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue

        # We can also make multiple licenses reserved at once.
        loans = []
        holds = []
        for i in range(3):
            p = db.patron()
            loan, _ = opds2_with_odl_api_fixture.checkout(patron=p)
            loans.append((loan, p))
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue

        l = db.license(
            opds2_with_odl_api_fixture.pool, terms_concurrency=2, checkouts_available=2
        )
        licenses.append(l)
        for i in range(3):
            hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
                db.patron(),
                start=utc_now() - datetime.timedelta(days=3 - i),
                position=i + 1,
            )
            holds.append(hold)

        opds2_with_odl_api_fixture.api.update_licensepool(
            opds2_with_odl_api_fixture.pool
        )
        assert 2 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 3 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 0 == holds[0].position
        assert 0 == holds[1].position
        assert 3 == holds[2].position
        assert holds[0].end - utc_now() - datetime.timedelta(
            days=3
        ) < datetime.timedelta(hours=1)
        assert holds[1].end - utc_now() - datetime.timedelta(
            days=3
        ) < datetime.timedelta(hours=1)

        # If there are more licenses that change than holds, some of them become available.
        for i in range(2):
            _, p = loans[i]
            opds2_with_odl_api_fixture.checkin(patron=p)
        assert 3 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 3 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        for hold in holds:
            assert 0 == hold.position
            assert hold.end - utc_now() - datetime.timedelta(
                days=3
            ) < datetime.timedelta(hours=1)

    def test_place_hold_success(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        loan, _ = opds2_with_odl_api_fixture.checkout(patron=db.patron())

        hold = opds2_with_odl_api_fixture.api.place_hold(
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
            "notifications@librarysimplified.org",
        )

        assert 1 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert opds2_with_odl_api_fixture.collection == hold.collection(db.session)
        assert opds2_with_odl_api_fixture.pool.data_source.name == hold.data_source_name
        assert opds2_with_odl_api_fixture.pool.identifier.type == hold.identifier_type
        assert opds2_with_odl_api_fixture.pool.identifier.identifier == hold.identifier
        assert hold.start_date is not None
        assert hold.start_date > utc_now() - datetime.timedelta(minutes=1)
        assert hold.start_date < utc_now() + datetime.timedelta(minutes=1)
        assert loan.end_date == hold.end_date
        assert 1 == hold.hold_position

    def test_place_hold_already_on_hold(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture
    ) -> None:
        opds2_with_odl_api_fixture.setup_license(concurrency=1, available=0)
        opds2_with_odl_api_fixture.pool.on_hold_to(opds2_with_odl_api_fixture.patron)
        pytest.raises(
            AlreadyOnHold,
            opds2_with_odl_api_fixture.api.place_hold,
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
            "notifications@librarysimplified.org",
        )

    def test_place_hold_currently_available(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture
    ) -> None:
        pytest.raises(
            CurrentlyAvailable,
            opds2_with_odl_api_fixture.api.place_hold,
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
            "notifications@librarysimplified.org",
        )

    def test_release_hold_success(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        loan_patron = db.patron()
        opds2_with_odl_api_fixture.checkout(patron=loan_patron)
        opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, position=1
        )

        opds2_with_odl_api_fixture.api.release_hold(
            opds2_with_odl_api_fixture.patron, "pin", opds2_with_odl_api_fixture.pool
        )
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 0 == db.session.query(Hold).count()

        opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, position=0
        )
        opds2_with_odl_api_fixture.checkin(patron=loan_patron)

        opds2_with_odl_api_fixture.api.release_hold(
            opds2_with_odl_api_fixture.patron, "pin", opds2_with_odl_api_fixture.pool
        )
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 0 == db.session.query(Hold).count()

        opds2_with_odl_api_fixture.pool.on_hold_to(
            opds2_with_odl_api_fixture.patron, position=0
        )
        other_hold, ignore = opds2_with_odl_api_fixture.pool.on_hold_to(
            db.patron(), position=2
        )

        opds2_with_odl_api_fixture.api.release_hold(
            opds2_with_odl_api_fixture.patron, "pin", opds2_with_odl_api_fixture.pool
        )
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 1 == opds2_with_odl_api_fixture.pool.patrons_in_hold_queue
        assert 1 == db.session.query(Hold).count()
        assert 0 == other_hold.position

    def test_release_hold_not_on_hold(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture
    ) -> None:
        pytest.raises(
            NotOnHold,
            opds2_with_odl_api_fixture.api.release_hold,
            opds2_with_odl_api_fixture.patron,
            "pin",
            opds2_with_odl_api_fixture.pool,
        )

    def test_patron_activity_loan(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        # No loans yet.
        assert [] == opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )

        # One loan.
        _, loan = opds2_with_odl_api_fixture.checkout()

        activity = opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )
        assert 1 == len(activity)
        assert opds2_with_odl_api_fixture.collection == activity[0].collection(
            db.session
        )
        assert (
            opds2_with_odl_api_fixture.pool.data_source.name
            == activity[0].data_source_name
        )
        assert (
            opds2_with_odl_api_fixture.pool.identifier.type
            == activity[0].identifier_type
        )
        assert (
            opds2_with_odl_api_fixture.pool.identifier.identifier
            == activity[0].identifier
        )
        assert loan.start == activity[0].start_date
        assert loan.end == activity[0].end_date
        assert loan.external_identifier == activity[0].external_identifier

        # Two loans.
        pool2 = db.licensepool(None, collection=opds2_with_odl_api_fixture.collection)
        license2 = db.license(pool2, terms_concurrency=1, checkouts_available=1)
        _, loan2 = opds2_with_odl_api_fixture.checkout(pool=pool2)

        def activity_sort_key(activity: LoanInfo | HoldInfo) -> datetime.datetime:
            if activity.start_date is None:
                raise TypeError("start_date is None")
            return activity.start_date

        activity = opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )
        assert 2 == len(activity)
        [l1, l2] = sorted(activity, key=activity_sort_key)

        assert opds2_with_odl_api_fixture.collection == l1.collection(db.session)
        assert opds2_with_odl_api_fixture.pool.data_source.name == l1.data_source_name
        assert opds2_with_odl_api_fixture.pool.identifier.type == l1.identifier_type
        assert opds2_with_odl_api_fixture.pool.identifier.identifier == l1.identifier
        assert loan.start == l1.start_date
        assert loan.end == l1.end_date
        assert loan.external_identifier == l1.external_identifier

        assert opds2_with_odl_api_fixture.collection == l2.collection(db.session)
        assert pool2.data_source.name == l2.data_source_name
        assert pool2.identifier.type == l2.identifier_type
        assert pool2.identifier.identifier == l2.identifier
        assert loan2.start == l2.start_date
        assert loan2.end == l2.end_date
        assert loan2.external_identifier == l2.external_identifier

        # If a loan is expired already, it's left out.
        loan2.end = utc_now() - datetime.timedelta(days=2)
        activity = opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )
        assert 1 == len(activity)
        assert (
            opds2_with_odl_api_fixture.pool.identifier.identifier
            == activity[0].identifier
        )
        opds2_with_odl_api_fixture.checkin(pool=pool2)

        # Open access loans are included.
        oa_work = db.work(
            with_open_access_download=True,
            collection=opds2_with_odl_api_fixture.collection,
        )
        pool3 = oa_work.license_pools[0]
        loan3, ignore = pool3.loan_to(opds2_with_odl_api_fixture.patron)

        activity = opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )
        assert 2 == len(activity)
        [l1, l2] = sorted(activity, key=activity_sort_key)

        assert opds2_with_odl_api_fixture.collection == l1.collection(db.session)
        assert opds2_with_odl_api_fixture.pool.data_source.name == l1.data_source_name
        assert opds2_with_odl_api_fixture.pool.identifier.type == l1.identifier_type
        assert opds2_with_odl_api_fixture.pool.identifier.identifier == l1.identifier
        assert loan.start == l1.start_date
        assert loan.end == l1.end_date
        assert loan.external_identifier == l1.external_identifier

        assert opds2_with_odl_api_fixture.collection == l2.collection(db.session)
        assert pool3.data_source.name == l2.data_source_name
        assert pool3.identifier.type == l2.identifier_type
        assert pool3.identifier.identifier == l2.identifier
        assert loan3.start == l2.start_date
        assert loan3.end == l2.end_date
        assert loan3.external_identifier == l2.external_identifier

        # remove the open access loan
        db.session.delete(loan3)

        # One hold.
        other_patron = db.patron()
        opds2_with_odl_api_fixture.checkout(patron=other_patron, pool=pool2)
        hold, _ = pool2.on_hold_to(opds2_with_odl_api_fixture.patron)
        hold.start = utc_now() - datetime.timedelta(days=2)
        hold.end = hold.start + datetime.timedelta(days=3)
        hold.position = 3
        activity = opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )
        assert 2 == len(activity)
        [h1, l1] = sorted(activity, key=activity_sort_key)

        assert isinstance(h1, HoldInfo)

        assert opds2_with_odl_api_fixture.collection == h1.collection(db.session)
        assert pool2.data_source.name == h1.data_source_name
        assert pool2.identifier.type == h1.identifier_type
        assert pool2.identifier.identifier == h1.identifier
        assert hold.start == h1.start_date
        assert hold.end == h1.end_date
        # Hold position was updated.
        assert 1 == h1.hold_position
        assert 1 == hold.position

        # If the hold is expired, it's deleted right away and the license
        # is made available again.
        opds2_with_odl_api_fixture.checkin(patron=other_patron, pool=pool2)
        hold.end = utc_now() - datetime.timedelta(days=1)
        hold.position = 0
        activity = opds2_with_odl_api_fixture.api.patron_activity(
            opds2_with_odl_api_fixture.patron, "pin"
        )
        assert 1 == len(activity)
        assert 0 == db.session.query(Hold).count()
        assert 1 == pool2.licenses_available
        assert 0 == pool2.licenses_reserved

    def test_update_loan_still_active(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        opds2_with_odl_api_fixture.setup_license(concurrency=6, available=6)
        loan, _ = opds2_with_odl_api_fixture.license.loan_to(
            opds2_with_odl_api_fixture.patron
        )
        loan.external_identifier = db.fresh_str()
        status_doc = opds2_with_odl_api_fixture.loan_status_document("active")

        opds2_with_odl_api_fixture.api.update_loan(loan, status_doc)
        # Availability hasn't changed, and the loan still exists.
        assert 6 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 1 == db.session.query(Loan).count()

    def test_update_loan_removes_loan(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        opds2_with_odl_api_fixture.setup_license(concurrency=7, available=7)
        _, loan = opds2_with_odl_api_fixture.checkout()

        assert 6 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 1 == db.session.query(Loan).count()

        status_doc = opds2_with_odl_api_fixture.loan_status_document("cancelled")

        opds2_with_odl_api_fixture.api.update_loan(loan, status_doc)

        # Availability has increased, and the loan is gone.
        assert 7 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 0 == db.session.query(Loan).count()

    def test_update_loan_removes_loan_with_hold_queue(
        self,
        db: DatabaseTransactionFixture,
        opds2_with_odl_api_fixture: OPDS2WithODLApiFixture,
    ) -> None:
        _, loan = opds2_with_odl_api_fixture.checkout()
        hold, _ = opds2_with_odl_api_fixture.pool.on_hold_to(db.patron(), position=1)
        opds2_with_odl_api_fixture.pool.update_availability_from_licenses()

        assert opds2_with_odl_api_fixture.pool.licenses_owned == 1
        assert opds2_with_odl_api_fixture.pool.licenses_available == 0
        assert opds2_with_odl_api_fixture.pool.licenses_reserved == 0
        assert opds2_with_odl_api_fixture.pool.patrons_in_hold_queue == 1

        status_doc = opds2_with_odl_api_fixture.loan_status_document("cancelled")

        opds2_with_odl_api_fixture.api.update_loan(loan, status_doc)

        # The license is reserved for the next patron, and the loan is gone.
        assert 0 == opds2_with_odl_api_fixture.pool.licenses_available
        assert 1 == opds2_with_odl_api_fixture.pool.licenses_reserved
        assert 0 == hold.position
        assert 0 == db.session.query(Loan).count()

    def test_settings_properties(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture
    ):
        real_api = OPDS2WithODLApi(
            opds2_with_odl_api_fixture.db.session, opds2_with_odl_api_fixture.collection
        )
        assert real_api._auth_type == OPDS2AuthType.BASIC
        assert real_api._username == "a"
        assert real_api._password == "b"
        assert real_api._feed_url == "http://odl"

    def test_can_fulfill_without_loan(
        self, opds2_with_odl_api_fixture: OPDS2WithODLApiFixture
    ):
        assert not opds2_with_odl_api_fixture.api.can_fulfill_without_loan(
            MagicMock(), MagicMock(), MagicMock()
        )
