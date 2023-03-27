import datetime
import json
from typing import Callable, List, Tuple

import pytest
from freezegun import freeze_time
from webpub_manifest_parser.core.ast import PresentationMetadata
from webpub_manifest_parser.odl.ast import ODLPublication
from webpub_manifest_parser.odl.semantic import (
    ODL_PUBLICATION_MUST_CONTAIN_EITHER_LICENSES_OR_OA_ACQUISITION_LINK_ERROR,
)

from api.circulation_exceptions import PatronHoldLimitReached, PatronLoanLimitReached
from api.odl2 import ODL2API, ODL2APIConfiguration, ODL2Importer
from core.coverage import CoverageFailure
from core.model import (
    Contribution,
    Contributor,
    DeliveryMechanism,
    Edition,
    EditionConstants,
    LicensePool,
    MediaTypes,
    Work,
    create,
)
from core.model.configuration import ConfigurationFactory, ConfigurationStorage
from core.model.constants import IdentifierConstants
from core.model.patron import Hold
from core.model.resource import Hyperlink
from tests.api.test_odl import LicenseHelper, LicenseInfoHelper, TestODLImporter
from tests.fixtures.api_odl2_files import ODL2APIFilesFixture
from tests.fixtures.database import DatabaseTransactionFixture
from tests.fixtures.odl import ODL2APITestFixture, ODLTestFixture


class TestODL2Importer(TestODLImporter):
    @staticmethod
    def _get_delivery_mechanism_by_drm_scheme_and_content_type(
        delivery_mechanisms, content_type, drm_scheme
    ):
        """Find a license pool in the list by its identifier.

        :param delivery_mechanisms: List of delivery mechanisms
        :type delivery_mechanisms: List[DeliveryMechanism]

        :param content_type: Content type
        :type content_type: str

        :param drm_scheme: DRM scheme
        :type drm_scheme: str

        :return: Delivery mechanism with the the specified DRM scheme and content type (if any)
        :rtype: Optional[DeliveryMechanism]
        """
        for delivery_mechanism in delivery_mechanisms:
            delivery_mechanism = delivery_mechanism.delivery_mechanism

            if (
                delivery_mechanism.drm_scheme == drm_scheme
                and delivery_mechanism.content_type == content_type
            ):
                return delivery_mechanism

        return None

    @pytest.fixture
    def integration_protocol(self):
        return ODL2API.NAME

    @pytest.fixture()
    def import_templated(  # type: ignore
        self,
        mock_get,
        importer,
        feed_template: str,
        api_odl2_files_fixture: ODL2APIFilesFixture,
    ) -> Callable:
        def i(licenses: List[LicenseInfoHelper]) -> Tuple[List, List, List, List]:
            feed_licenses = [l.license for l in licenses]
            [mock_get.add(l) for l in licenses]
            feed = self.get_templated_feed(
                files=api_odl2_files_fixture,
                filename=feed_template,
                licenses=feed_licenses,
            )
            return importer.import_from_feed(feed)

        return i

    @pytest.fixture()
    def importer(
        self,
        db: DatabaseTransactionFixture,
        odl_test_fixture: ODLTestFixture,
        mock_get,
    ) -> ODL2Importer:
        library = odl_test_fixture.library()
        return ODL2Importer(
            db.session,
            collection=odl_test_fixture.collection(library),
            http_get=mock_get.get,
        )

    @pytest.fixture()
    def feed_template(self):
        return "feed_template.json.jinja"

    @freeze_time("2016-01-01T00:00:00+00:00")
    def test_import(
        self,
        db: DatabaseTransactionFixture,
        importer,
        mock_get,
        datasource,
        api_odl2_files_fixture: ODL2APIFilesFixture,
    ):
        """Ensure that ODL2Importer2 correctly processes and imports the ODL feed encoded using OPDS 2.x.

        NOTE: `freeze_time` decorator is required to treat the licenses in the ODL feed as non-expired.
        """
        # Arrange
        moby_dick_license = LicenseInfoHelper(
            license=LicenseHelper(
                identifier="urn:uuid:f7847120-fc6f-11e3-8158-56847afe9799",
                concurrency=10,
                checkouts=30,
                expires="2016-04-25T12:25:21+02:00",
            ),
            left=30,
            available=10,
        )

        mock_get.add(moby_dick_license)
        feed = api_odl2_files_fixture.sample_text("feed.json")

        configuration_storage = ConfigurationStorage(importer)
        configuration_factory = ConfigurationFactory()

        with configuration_factory.create(
            configuration_storage, db.session, ODL2APIConfiguration
        ) as configuration:
            configuration.set_ignored_identifier_types([IdentifierConstants.URI])
            configuration.skipped_license_formats = json.dumps(["text/html"])

        # Act
        imported_editions, pools, works, failures = importer.import_from_feed(feed)

        # Assert

        # 1. Make sure that there is a single edition only
        assert isinstance(imported_editions, list)
        assert 1 == len(imported_editions)

        [moby_dick_edition] = imported_editions
        assert isinstance(moby_dick_edition, Edition)
        assert moby_dick_edition.primary_identifier.identifier == "978-3-16-148410-0"
        assert moby_dick_edition.primary_identifier.type == "ISBN"
        assert Hyperlink.SAMPLE in {
            l.rel for l in moby_dick_edition.primary_identifier.links
        }

        assert "Moby-Dick" == moby_dick_edition.title
        assert "eng" == moby_dick_edition.language
        assert "eng" == moby_dick_edition.language
        assert EditionConstants.BOOK_MEDIUM == moby_dick_edition.medium
        assert "Herman Melville" == moby_dick_edition.author

        assert 1 == len(moby_dick_edition.author_contributors)
        [moby_dick_author] = moby_dick_edition.author_contributors
        assert isinstance(moby_dick_author, Contributor)
        assert "Herman Melville" == moby_dick_author.display_name
        assert "Melville, Herman" == moby_dick_author.sort_name

        assert 1 == len(moby_dick_author.contributions)
        [moby_dick_author_author_contribution] = moby_dick_author.contributions
        assert isinstance(moby_dick_author_author_contribution, Contribution)
        assert moby_dick_author == moby_dick_author_author_contribution.contributor
        assert moby_dick_edition == moby_dick_author_author_contribution.edition
        assert Contributor.AUTHOR_ROLE == moby_dick_author_author_contribution.role

        assert datasource == moby_dick_edition.data_source

        assert "Test Publisher" == moby_dick_edition.publisher
        assert datetime.date(2015, 9, 29) == moby_dick_edition.published

        assert "http://example.org/cover.jpg" == moby_dick_edition.cover_full_url
        assert (
            "http://example.org/cover-small.jpg"
            == moby_dick_edition.cover_thumbnail_url
        )

        # 2. Make sure that license pools have correct configuration
        assert isinstance(pools, list)
        assert 1 == len(pools)

        [moby_dick_license_pool] = pools
        assert isinstance(moby_dick_license_pool, LicensePool)
        assert moby_dick_license_pool.identifier.identifier == "978-3-16-148410-0"  # type: ignore
        assert moby_dick_license_pool.identifier.type == "ISBN"  # type: ignore
        assert not moby_dick_license_pool.open_access
        assert 30 == moby_dick_license_pool.licenses_owned
        assert 10 == moby_dick_license_pool.licenses_available

        assert 2 == len(moby_dick_license_pool.delivery_mechanisms)

        moby_dick_epub_adobe_drm_delivery_mechanism = (
            self._get_delivery_mechanism_by_drm_scheme_and_content_type(
                moby_dick_license_pool.delivery_mechanisms,
                MediaTypes.EPUB_MEDIA_TYPE,
                DeliveryMechanism.ADOBE_DRM,
            )
        )
        assert moby_dick_epub_adobe_drm_delivery_mechanism is not None

        moby_dick_epub_lcp_drm_delivery_mechanism = (
            self._get_delivery_mechanism_by_drm_scheme_and_content_type(
                moby_dick_license_pool.delivery_mechanisms,
                MediaTypes.EPUB_MEDIA_TYPE,
                DeliveryMechanism.LCP_DRM,
            )
        )
        assert moby_dick_epub_lcp_drm_delivery_mechanism is not None

        assert 1 == len(moby_dick_license_pool.licenses)
        [moby_dick_license] = moby_dick_license_pool.licenses  # type: ignore
        assert (
            "urn:uuid:f7847120-fc6f-11e3-8158-56847afe9799"
            == moby_dick_license.identifier  # type: ignore
        )
        assert (
            "http://www.example.com/get{?id,checkout_id,expires,patron_id,passphrase,hint,hint_url,notification_url}"
            == moby_dick_license.checkout_url  # type: ignore
        )
        assert "http://www.example.com/status/294024" == moby_dick_license.status_url  # type: ignore
        assert (
            datetime.datetime(2016, 4, 25, 10, 25, 21, tzinfo=datetime.timezone.utc)
            == moby_dick_license.expires  # type: ignore
        )
        assert 30 == moby_dick_license.checkouts_left  # type: ignore
        assert 10 == moby_dick_license.checkouts_available  # type: ignore

        # 3. Make sure that work objects contain all the required metadata
        assert isinstance(works, list)
        assert 1 == len(works)

        [moby_dick_work] = works
        assert isinstance(moby_dick_work, Work)
        assert moby_dick_edition == moby_dick_work.presentation_edition
        assert 1 == len(moby_dick_work.license_pools)
        assert moby_dick_license_pool == moby_dick_work.license_pools[0]

        # 4. Make sure that the failure is covered
        assert 1 == len(failures)
        huck_finn_failures = failures["9781234567897"]

        assert 1 == len(huck_finn_failures)
        [huck_finn_failure] = huck_finn_failures
        assert isinstance(huck_finn_failure, CoverageFailure)
        assert "9781234567897" == huck_finn_failure.obj.identifier

        huck_finn_semantic_error = (
            ODL_PUBLICATION_MUST_CONTAIN_EITHER_LICENSES_OR_OA_ACQUISITION_LINK_ERROR(
                node=ODLPublication(
                    metadata=PresentationMetadata(identifier="urn:isbn:9781234567897")
                ),
                node_property=None,
            )
        )
        assert str(huck_finn_semantic_error) == huck_finn_failure.exception

    @freeze_time("2016-01-01T00:00:00+00:00")
    def test_import_audiobook_with_streaming(
        self,
        db: DatabaseTransactionFixture,
        importer,
        mock_get,
        datasource,
        api_odl2_files_fixture: ODL2APIFilesFixture,
    ):
        """Ensure that ODL2Importer2 correctly processes and imports a feed with an audiobook."""
        license = api_odl2_files_fixture.sample_text("license-audiobook.json")
        feed = api_odl2_files_fixture.sample_text("feed-audiobook-streaming.json")
        mock_get.add(license)

        configuration_storage = ConfigurationStorage(importer)
        configuration_factory = ConfigurationFactory()

        with configuration_factory.create(
            configuration_storage, db.session, ODL2APIConfiguration
        ) as configuration:
            configuration.skipped_license_formats = json.dumps(["text/html"])

        imported_editions, pools, works, failures = importer.import_from_feed(feed)

        # Make sure we imported one edition and it is an audiobook
        assert isinstance(imported_editions, list)
        assert 1 == len(imported_editions)

        [edition] = imported_editions
        assert isinstance(edition, Edition)
        assert edition.primary_identifier.identifier == "9780792766919"
        assert edition.primary_identifier.type == "ISBN"
        assert EditionConstants.AUDIO_MEDIUM == edition.medium

        # Make sure that license pools have correct configuration
        assert isinstance(pools, list)
        assert 1 == len(pools)

        [license_pool] = pools
        assert not license_pool.open_access
        assert 1 == license_pool.licenses_owned
        assert 1 == license_pool.licenses_available

        assert 2 == len(license_pool.delivery_mechanisms)

        lcp_delivery_mechanism = (
            self._get_delivery_mechanism_by_drm_scheme_and_content_type(
                license_pool.delivery_mechanisms,
                MediaTypes.AUDIOBOOK_PACKAGE_LCP_MEDIA_TYPE,
                DeliveryMechanism.LCP_DRM,
            )
        )
        assert lcp_delivery_mechanism is not None

        feedbooks_delivery_mechanism = (
            self._get_delivery_mechanism_by_drm_scheme_and_content_type(
                license_pool.delivery_mechanisms,
                MediaTypes.AUDIOBOOK_MANIFEST_MEDIA_TYPE,
                DeliveryMechanism.FEEDBOOKS_AUDIOBOOK_DRM,
            )
        )
        assert feedbooks_delivery_mechanism is not None

    @freeze_time("2016-01-01T00:00:00+00:00")
    def test_import_audiobook_no_streaming(
        self,
        db: DatabaseTransactionFixture,
        importer,
        mock_get,
        datasource,
        api_odl2_files_fixture: ODL2APIFilesFixture,
    ):
        """
        Ensure that ODL2Importer2 correctly processes and imports a feed with an audiobook
        that is not available for streaming.
        """
        license = api_odl2_files_fixture.sample_text("license-audiobook.json")
        feed = api_odl2_files_fixture.sample_text("feed-audiobook-no-streaming.json")
        mock_get.add(license)

        imported_editions, pools, works, failures = importer.import_from_feed(feed)

        # Make sure we imported one edition and it is an audiobook
        assert isinstance(imported_editions, list)
        assert 1 == len(imported_editions)

        [edition] = imported_editions
        assert isinstance(edition, Edition)
        assert edition.primary_identifier.identifier == "9781603937221"
        assert edition.primary_identifier.type == "ISBN"
        assert EditionConstants.AUDIO_MEDIUM == edition.medium

        # Make sure that license pools have correct configuration
        assert isinstance(pools, list)
        assert 1 == len(pools)

        [license_pool] = pools
        assert not license_pool.open_access
        assert 1 == license_pool.licenses_owned
        assert 1 == license_pool.licenses_available

        assert 1 == len(license_pool.delivery_mechanisms)

        lcp_delivery_mechanism = (
            self._get_delivery_mechanism_by_drm_scheme_and_content_type(
                license_pool.delivery_mechanisms,
                MediaTypes.AUDIOBOOK_PACKAGE_LCP_MEDIA_TYPE,
                DeliveryMechanism.LCP_DRM,
            )
        )
        assert lcp_delivery_mechanism is not None


class TestODL2API:
    def test_loan_limit(self, odl2_api_test_fixture: ODL2APITestFixture):
        """Test the loan limit collection setting"""
        odl2api = odl2_api_test_fixture
        # Set the loan limit
        odl2api.api.loan_limit = 1

        response = odl2api.checkout(
            patron=odl2api.patron, pool=odl2api.work.active_license_pool()
        )
        # Did the loan take place correctly?
        assert (
            response[0].identifier
            == odl2api.work.presentation_edition.primary_identifier.identifier
        )

        # Second loan for the patron should fail due to the loan limit
        work2: Work = odl2api.fixture.work(odl2api.collection)
        with pytest.raises(PatronLoanLimitReached) as exc:
            odl2api.checkout(patron=odl2api.patron, pool=work2.active_license_pool())
        assert exc.value.limit == 1

    def test_hold_limit(
        self, db: DatabaseTransactionFixture, odl2_api_test_fixture: ODL2APITestFixture
    ):
        """Test the hold limit collection setting"""
        odl2api = odl2_api_test_fixture
        # Set the hold limit
        odl2api.api.hold_limit = 1

        patron1 = db.patron()

        # First checkout with patron1, then place a hold with the test patron
        pool = odl2api.work.active_license_pool()
        response = odl2api.checkout(patron=patron1, pool=pool)
        assert (
            response[0].identifier
            == odl2api.work.presentation_edition.primary_identifier.identifier
        )

        response = odl2api.api.place_hold(odl2api.patron, "pin", pool, "")
        # Hold was successful
        assert response.hold_position == 1
        create(db.session, Hold, patron_id=odl2api.patron.id, license_pool=pool)

        # Second work should fail for the test patron due to the hold limit
        work2: Work = odl2api.fixture.work(odl2api.collection)
        # Generate a license
        odl2api.fixture.license(work2)

        # Do the same, patron1 checkout and test patron hold
        pool = work2.active_license_pool()
        response = odl2api.checkout(patron=patron1, pool=pool)
        assert (
            response[0].identifier
            == work2.presentation_edition.primary_identifier.identifier
        )

        # Hold should fail
        with pytest.raises(PatronHoldLimitReached) as exc:
            odl2api.api.place_hold(odl2api.patron, "pin", pool, "")
        assert exc.value.limit == 1
