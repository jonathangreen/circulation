from __future__ import annotations

import datetime
import json
import os
import random
import stat
import tempfile
from io import StringIO
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from api.lanes import create_default_lanes
from core.classifier import Classifier
from core.config import CannotLoadConfiguration
from core.external_search import Filter, MockExternalSearchIndex
from core.lane import Lane, WorkList
from core.metadata_layer import LinkData, TimestampData
from core.mirror import MirrorUploader
from core.model import (
    CachedFeed,
    Collection,
    ConfigurationSetting,
    Contributor,
    CoverageRecord,
    DataSource,
    ExternalIntegration,
    Hyperlink,
    Identifier,
    Library,
    RightsStatus,
    Timestamp,
    Work,
    WorkCoverageRecord,
    create,
    get_one,
)
from core.model.classification import Subject
from core.model.configuration import ExternalIntegrationLink
from core.model.customlist import CustomList
from core.monitor import CollectionMonitor, Monitor, ReaperMonitor
from core.opds_import import OPDSImportMonitor
from core.s3 import MinIOUploader, MinIOUploaderConfiguration, S3Uploader
from core.scripts import (
    AddClassificationScript,
    CheckContributorNamesInDB,
    CollectionArgumentsScript,
    CollectionInputScript,
    CollectionType,
    ConfigureCollectionScript,
    ConfigureIntegrationScript,
    ConfigureLaneScript,
    ConfigureLibraryScript,
    ConfigureSiteScript,
    CustomListUpdateEntriesScript,
    DatabaseMigrationInitializationScript,
    DatabaseMigrationScript,
    DeleteInvisibleLanesScript,
    Explain,
    IdentifierInputScript,
    LaneSweeperScript,
    LibraryInputScript,
    ListCollectionMetadataIdentifiersScript,
    MirrorResourcesScript,
    MockStdin,
    OPDSImportScript,
    PatronInputScript,
    RebuildSearchIndexScript,
    ReclassifyWorksForUncheckedSubjectsScript,
    RunCollectionMonitorScript,
    RunCoverageProviderScript,
    RunMonitorScript,
    RunMultipleMonitorsScript,
    RunReaperMonitorsScript,
    RunThreadedCollectionCoverageProviderScript,
    RunWorkCoverageProviderScript,
    Script,
    SearchIndexCoverageRemover,
    ShowCollectionsScript,
    ShowIntegrationsScript,
    ShowLanesScript,
    ShowLibrariesScript,
    TimestampScript,
    UpdateCustomListSizeScript,
    UpdateLaneSizeScript,
    WhereAreMyBooksScript,
    WorkClassificationScript,
    WorkProcessingScript,
)
from core.testing import (
    AlwaysSuccessfulCollectionCoverageProvider,
    AlwaysSuccessfulWorkCoverageProvider,
)
from core.util.datetime_helpers import datetime_utc, strptime_utc, utc_now
from core.util.worker_pools import DatabasePool
from tests.fixtures.database import DatabaseTransactionFixture
from tests.fixtures.search import EndToEndSearchFixture, ExternalSearchPatchFixture


class TestScript:
    def test_parse_time(self):
        reference_date = datetime_utc(2016, 1, 1)

        assert Script.parse_time("2016-01-01") == reference_date
        assert Script.parse_time("2016-1-1") == reference_date
        assert Script.parse_time("1/1/2016") == reference_date
        assert Script.parse_time("20160101") == reference_date

        pytest.raises(ValueError, Script.parse_time, "201601-01")

    def test_script_name(self, db: DatabaseTransactionFixture):
        session = db.session

        class Sample(Script):
            pass

        # If a script does not define .name, its class name
        # is treated as the script name.
        script = Sample(session)
        assert "Sample" == script.script_name

        # If a script does define .name, that's used instead.
        script.name = "I'm a script"
        assert script.name == script.script_name


class TestTimestampScript:
    @staticmethod
    def _ts(session: Session, script):
        """Convenience method to look up the Timestamp for a script.

        We don't use Timestamp.stamp() because we want to make sure
        that Timestamps are being created by the actual code, not test
        code.
        """
        return get_one(session, Timestamp, service=script.script_name)

    def test_update_timestamp(self, db: DatabaseTransactionFixture):
        # Test the Script subclass that sets a timestamp after a
        # script is run.
        class Noisy(TimestampScript):
            def do_run(self):
                pass

        script = Noisy(db.session)
        script.run()

        timestamp = self._ts(db.session, script)

        # The start and end points of do_run() have become
        # Timestamp.start and Timestamp.finish.
        now = utc_now()
        assert (now - timestamp.start).total_seconds() < 5
        assert (now - timestamp.finish).total_seconds() < 5
        assert timestamp.start < timestamp.finish
        assert None == timestamp.collection

    def test_update_timestamp_with_collection(self, db: DatabaseTransactionFixture):
        # A script can indicate that it is operating on a specific
        # collection.
        class MyCollection(TimestampScript):
            def do_run(self):
                pass

        script = MyCollection(db.session)
        script.timestamp_collection = db.default_collection()
        script.run()
        timestamp = self._ts(db.session, script)
        assert db.default_collection() == timestamp.collection

    def test_update_timestamp_on_failure(self, db: DatabaseTransactionFixture):
        # A TimestampScript that fails to complete still has its
        # Timestamp set -- the timestamp just records the time that
        # the script stopped running.
        #
        # This is different from Monitors, where the timestamp
        # is only updated when the Monitor runs to completion.
        # The difference is that Monitors are frequently responsible for
        # keeping track of everything that happened since a certain
        # time, and Scripts generally aren't.
        class Broken(TimestampScript):
            def do_run(self):
                raise Exception("i'm broken")

        script = Broken(db.session)
        with pytest.raises(Exception) as excinfo:
            script.run()
        assert "i'm broken" in str(excinfo.value)
        timestamp = self._ts(db.session, script)

        now = utc_now()
        assert (now - timestamp.finish).total_seconds() < 5

        # A stack trace for the exception has been recorded in the
        # Timestamp object.
        assert "Exception: i'm broken" in timestamp.exception

    def test_normal_script_has_no_timestamp(self, db: DatabaseTransactionFixture):
        # Running a normal script does _not_ set a Timestamp.
        class Silent(Script):
            def do_run(self):
                pass

        script = Silent(db.session)
        script.run()
        assert None == self._ts(db.session, script)


class TestCheckContributorNamesInDB:
    def test_process_contribution_local(self, db: DatabaseTransactionFixture):
        stdin = MockStdin()
        cmd_args = []

        edition_alice, pool_alice = db.edition(
            data_source_name=DataSource.GUTENBERG,
            identifier_type=Identifier.GUTENBERG_ID,
            identifier_id="1",
            with_open_access_download=True,
            title="Alice Writes Books",
        )

        alice, new = db.contributor(sort_name="Alice Alrighty")
        alice._sort_name = "Alice Alrighty"
        alice.display_name = "Alice Alrighty"

        edition_alice.add_contributor(alice, [Contributor.PRIMARY_AUTHOR_ROLE])
        edition_alice.sort_author = "Alice Rocks"

        # everything is set up as we expect
        assert "Alice Alrighty" == alice.sort_name
        assert "Alice Alrighty" == alice.display_name
        assert "Alice Rocks" == edition_alice.sort_author

        edition_bob, pool_bob = db.edition(
            data_source_name=DataSource.GUTENBERG,
            identifier_type=Identifier.GUTENBERG_ID,
            identifier_id="2",
            with_open_access_download=True,
            title="Bob Writes Books",
        )

        bob, new = db.contributor(sort_name="Bob")
        bob.display_name = "Bob Bitshifter"

        edition_bob.add_contributor(bob, [Contributor.PRIMARY_AUTHOR_ROLE])
        edition_bob.sort_author = "Bob Rocks"

        assert "Bob" == bob.sort_name
        assert "Bob Bitshifter" == bob.display_name
        assert "Bob Rocks" == edition_bob.sort_author

        contributor_fixer = CheckContributorNamesInDB(
            _db=db.session, cmd_args=cmd_args, stdin=stdin
        )
        contributor_fixer.do_run()

        # Alice got fixed up.
        assert "Alrighty, Alice" == alice.sort_name
        assert "Alice Alrighty" == alice.display_name
        assert "Alrighty, Alice" == edition_alice.sort_author

        # Bob's repairs were too extensive to make.
        assert "Bob" == bob.sort_name
        assert "Bob Bitshifter" == bob.display_name
        assert "Bob Rocks" == edition_bob.sort_author


class TestIdentifierInputScript:
    def test_parse_list_as_identifiers(self, db: DatabaseTransactionFixture):
        i1 = db.identifier()
        i2 = db.identifier()
        args = [i1.identifier, "no-such-identifier", i2.identifier]
        identifiers = IdentifierInputScript.parse_identifier_list(
            db.session, i1.type, None, args
        )
        assert [i1, i2] == identifiers

        assert [] == IdentifierInputScript.parse_identifier_list(
            db.session, i1.type, None, []
        )

    def test_parse_list_as_identifiers_with_autocreate(
        self, db: DatabaseTransactionFixture
    ):
        type = Identifier.OVERDRIVE_ID
        args = ["brand-new-identifier"]
        [i] = IdentifierInputScript.parse_identifier_list(
            db.session, type, None, args, autocreate=True
        )
        assert type == i.type
        assert "brand-new-identifier" == i.identifier

    def test_parse_list_as_identifiers_with_data_source(
        self, db: DatabaseTransactionFixture
    ):
        lp1 = db.licensepool(None, data_source_name=DataSource.UNGLUE_IT)
        lp2 = db.licensepool(None, data_source_name=DataSource.FEEDBOOKS)
        lp3 = db.licensepool(None, data_source_name=DataSource.FEEDBOOKS)

        i1, i2, i3 = (lp.identifier for lp in [lp1, lp2, lp3])
        i1.type = i2.type = Identifier.URI
        source = DataSource.lookup(db.session, DataSource.FEEDBOOKS)

        # Only URIs with a FeedBooks LicensePool are selected.
        identifiers = IdentifierInputScript.parse_identifier_list(
            db.session, Identifier.URI, source, []
        )
        assert [i2] == identifiers

    def test_parse_list_as_identifiers_by_database_id(
        self, db: DatabaseTransactionFixture
    ):
        id1 = db.identifier()
        id2 = db.identifier()

        # Make a list containing two Identifier database IDs,
        # as well as two strings which are not existing Identifier database
        # IDs.
        ids = [id1.id, "10000000", "abcde", id2.id]

        identifiers = IdentifierInputScript.parse_identifier_list(
            db.session, IdentifierInputScript.DATABASE_ID, None, ids
        )
        assert [id1, id2] == identifiers

    def test_parse_command_line(self, db: DatabaseTransactionFixture):
        i1 = db.identifier()
        i2 = db.identifier()
        # We pass in one identifier on the command line...
        cmd_args = ["--identifier-type", i1.type, i1.identifier]
        # ...and another one into standard input.
        stdin = MockStdin(i2.identifier)
        parsed = IdentifierInputScript.parse_command_line(db.session, cmd_args, stdin)
        assert [i1, i2] == parsed.identifiers
        assert i1.type == parsed.identifier_type

    def test_parse_command_line_no_identifiers(self, db: DatabaseTransactionFixture):
        cmd_args = [
            "--identifier-type",
            Identifier.OVERDRIVE_ID,
            "--identifier-data-source",
            DataSource.STANDARD_EBOOKS,
        ]
        parsed = IdentifierInputScript.parse_command_line(
            db.session, cmd_args, MockStdin()
        )
        assert [] == parsed.identifiers
        assert Identifier.OVERDRIVE_ID == parsed.identifier_type
        assert DataSource.STANDARD_EBOOKS == parsed.identifier_data_source


class SuccessMonitor(Monitor):
    """A simple Monitor that alway succeeds."""

    SERVICE_NAME = "Success"

    def run(self):
        self.ran = True


class OPDSCollectionMonitor(CollectionMonitor):
    """Mock Monitor for use in tests of Run*MonitorScript."""

    SERVICE_NAME = "Test Monitor"
    PROTOCOL = ExternalIntegration.OPDS_IMPORT

    def __init__(self, _db, test_argument=None, **kwargs):
        self.test_argument = test_argument
        super().__init__(_db, **kwargs)

    def run_once(self, progress):
        self.collection.ran_with_argument = self.test_argument


class DoomedCollectionMonitor(CollectionMonitor):
    """Mock CollectionMonitor that always raises an exception."""

    SERVICE_NAME = "Doomed Monitor"
    PROTOCOL = ExternalIntegration.OPDS_IMPORT

    def run(self, *args, **kwargs):
        self.ran = True
        self.collection.doomed = True
        raise Exception("Doomed!")


class TestCollectionMonitorWithDifferentRunners:
    """CollectionMonitors are usually run by a RunCollectionMonitorScript.
    It's not ideal, but you can also run a CollectionMonitor script from a
    RunMonitorScript. In either case, if no collection argument is specified,
    the monitor will run on every appropriate Collection. If any collection
    names are specified, then the monitor will be run only on the ones specified.
    """

    @pytest.mark.parametrize(
        "name,script_runner",
        [
            ("run CollectionMonitor from RunMonitorScript", RunMonitorScript),
            (
                "run CollectionMonitor from RunCollectionMonitorScript",
                RunCollectionMonitorScript,
            ),
        ],
    )
    def test_run_collection_monitor_with_no_args(self, db, name, script_runner):
        # Run CollectionMonitor via RunMonitor for all applicable collections.
        c1 = db.collection()
        c2 = db.collection()
        script = script_runner(
            OPDSCollectionMonitor, db.session, cmd_args=[], test_argument="test value"
        )
        script.run()
        for c in [c1, c2]:
            assert "test value" == c.ran_with_argument

    @pytest.mark.parametrize(
        "name,script_runner",
        [
            (
                "run CollectionMonitor with collection args from RunMonitorScript",
                RunMonitorScript,
            ),
            (
                "run CollectionMonitor with collection args from RunCollectionMonitorScript",
                RunCollectionMonitorScript,
            ),
        ],
    )
    def test_run_collection_monitor_with_collection_args(self, db, name, script_runner):
        # Run CollectionMonitor via RunMonitor for only specified collections.
        c1 = db.collection(name="Collection 1")
        c2 = db.collection(name="Collection 2")
        c3 = db.collection(name="Collection 3")

        all_collections = [c1, c2, c3]
        monitored_collections = [c1, c3]
        monitored_names = [c.name for c in monitored_collections]
        script = script_runner(
            OPDSCollectionMonitor,
            db.session,
            cmd_args=monitored_names,
            test_argument="test value",
        )
        script.run()
        for c in monitored_collections:
            assert hasattr(c, "ran_with_argument")
            assert "test value" == c.ran_with_argument
        for c in [
            collection
            for collection in all_collections
            if collection not in monitored_collections
        ]:
            assert not hasattr(c, "ran_with_argument")


class TestRunMultipleMonitorsScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        m1 = SuccessMonitor(db.session)
        m2 = DoomedCollectionMonitor(db.session, db.default_collection())
        m3 = SuccessMonitor(db.session)

        class MockScript(RunMultipleMonitorsScript):
            name = "Run three monitors"

            def monitors(self, **kwargs):
                self.kwargs = kwargs
                return [m1, m2, m3]

        # Run the script.
        script = MockScript(db.session, kwarg="value")
        script.do_run()

        # The kwarg we passed in to the MockScript constructor was
        # propagated into the monitors() method.
        assert dict(kwarg="value") == script.kwargs

        # All three monitors were run, even though the
        # second one raised an exception.
        assert True == m1.ran
        assert True == m2.ran
        assert True == m3.ran

        # The exception that crashed the second monitor was stored as
        # .exception, in case we want to look at it.
        assert "Doomed!" == str(m2.exception)
        assert None == getattr(m1, "exception", None)


class TestRunCollectionMonitorScript:
    def test_monitors(self, db: DatabaseTransactionFixture):
        # Here we have three OPDS import Collections...
        o1 = db.collection()
        o2 = db.collection()
        o3 = db.collection()

        # ...and a Bibliotheca collection.
        b1 = db.collection(protocol=ExternalIntegration.BIBLIOTHECA)

        script = RunCollectionMonitorScript(
            OPDSCollectionMonitor, db.session, cmd_args=[]
        )

        # Calling monitors() instantiates an OPDSCollectionMonitor
        # for every OPDS import collection. The Bibliotheca collection
        # is unaffected.
        monitors = script.monitors()
        collections = [x.collection for x in monitors]
        assert set(collections) == {o1, o2, o3}
        for monitor in monitors:
            assert isinstance(monitor, OPDSCollectionMonitor)


class TestRunReaperMonitorsScript:
    def test_monitors(self, db: DatabaseTransactionFixture):
        """This script instantiates a Monitor for every class in
        ReaperMonitor.REGISTRY.
        """
        old_registry = ReaperMonitor.REGISTRY
        ReaperMonitor.REGISTRY = [SuccessMonitor]
        script = RunReaperMonitorsScript(db.session)
        [monitor] = script.monitors()
        assert isinstance(monitor, SuccessMonitor)
        ReaperMonitor.REGISTRY = old_registry


class TestPatronInputScript:
    def test_parse_patron_list(self, db: DatabaseTransactionFixture):
        """Test that patrons can be identified with any unique identifier."""
        l1 = db.library()
        l2 = db.library()
        p1 = db.patron()
        p1.authorization_identifier = db.fresh_str()
        p1.library_id = l1.id
        p2 = db.patron()
        p2.username = db.fresh_str()
        p2.library_id = l1.id
        p3 = db.patron()
        p3.external_identifier = db.fresh_str()
        p3.library_id = l1.id
        p4 = db.patron()
        p4.external_identifier = db.fresh_str()
        p4.library_id = l2.id
        args = [
            p1.authorization_identifier,
            "no-such-patron",
            "",
            p2.username,
            p3.external_identifier,
        ]
        patrons = PatronInputScript.parse_patron_list(db.session, l1, args)
        assert [p1, p2, p3] == patrons
        assert [] == PatronInputScript.parse_patron_list(db.session, l1, [])
        assert [p1] == PatronInputScript.parse_patron_list(
            db.session, l1, [p1.external_identifier, p4.external_identifier]
        )
        assert [p4] == PatronInputScript.parse_patron_list(
            db.session, l2, [p1.external_identifier, p4.external_identifier]
        )

    def test_parse_command_line(self, db: DatabaseTransactionFixture):
        l1 = db.library()
        p1 = db.patron()
        p2 = db.patron()
        p1.authorization_identifier = db.fresh_str()
        p2.authorization_identifier = db.fresh_str()
        p1.library_id = l1.id
        p2.library_id = l1.id
        # We pass in one patron identifier on the command line...
        cmd_args = [l1.short_name, p1.authorization_identifier]
        # ...and another one into standard input.
        stdin = MockStdin(p2.authorization_identifier)
        parsed = PatronInputScript.parse_command_line(db.session, cmd_args, stdin)
        assert [p1, p2] == parsed.patrons

    def test_patron_different_library(self, db: DatabaseTransactionFixture):
        l1 = db.library()
        l2 = db.library()
        p1 = db.patron()
        p2 = db.patron()
        p1.authorization_identifier = db.fresh_str()
        p2.authorization_identifier = p1.authorization_identifier
        p1.library_id = l1.id
        p2.library_id = l2.id
        cmd_args = [l1.short_name, p1.authorization_identifier]
        parsed = PatronInputScript.parse_command_line(db.session, cmd_args, None)
        assert [p1] == parsed.patrons
        cmd_args = [l2.short_name, p2.authorization_identifier]
        parsed = PatronInputScript.parse_command_line(db.session, cmd_args, None)
        assert [p2] == parsed.patrons

    def test_do_run(self, db: DatabaseTransactionFixture):
        """Test that PatronInputScript.do_run() calls process_patron()
        for every patron designated by the command-line arguments.
        """

        class MockPatronInputScript(PatronInputScript):
            def process_patron(self, patron):
                patron.processed = True

        l1 = db.library()
        p1 = db.patron()
        p2 = db.patron()
        p3 = db.patron()
        p1.library_id = l1.id
        p2.library_id = l1.id
        p3.library_id = l1.id
        p1.processed = False
        p2.processed = False
        p3.processed = False
        p1.authorization_identifier = db.fresh_str()
        p2.authorization_identifier = db.fresh_str()
        cmd_args = [l1.short_name, p1.authorization_identifier]
        stdin = MockStdin(p2.authorization_identifier)
        script = MockPatronInputScript(db.session)
        script.do_run(cmd_args=cmd_args, stdin=stdin)
        assert True == p1.processed
        assert True == p2.processed
        assert False == p3.processed


class TestLibraryInputScript:
    def test_parse_library_list(self, db: DatabaseTransactionFixture):
        """Test that libraries can be identified with their full name or short name."""
        l1 = db.library()
        l2 = db.library()
        args = [l1.name, "no-such-library", "", l2.short_name]
        libraries = LibraryInputScript.parse_library_list(db.session, args)
        assert [l1, l2] == libraries

        assert [] == LibraryInputScript.parse_library_list(db.session, [])

    def test_parse_command_line(self, db: DatabaseTransactionFixture):
        l1 = db.library()
        # We pass in one library identifier on the command line...
        cmd_args = [l1.name]
        parsed = LibraryInputScript.parse_command_line(db.session, cmd_args)

        # And here it is.
        assert [l1] == parsed.libraries

    def test_parse_command_line_no_identifiers(self, db: DatabaseTransactionFixture):
        """If you don't specify any libraries on the command
        line, we will process all libraries in the system.
        """
        parsed = LibraryInputScript.parse_command_line(db.session, [])
        assert db.session.query(Library).all() == parsed.libraries

    def test_do_run(self, db: DatabaseTransactionFixture):
        """Test that LibraryInputScript.do_run() calls process_library()
        for every library designated by the command-line arguments.
        """

        class MockLibraryInputScript(LibraryInputScript):
            def process_library(self, library):
                library.processed = True

        l1 = db.library()
        l2 = db.library()
        l2.processed = False
        cmd_args = [l1.name]
        script = MockLibraryInputScript(db.session)
        script.do_run(cmd_args=cmd_args)
        assert True == l1.processed
        assert False == l2.processed


class TestLaneSweeperScript:
    def test_process_library(self, db: DatabaseTransactionFixture):
        class Mock(LaneSweeperScript):
            def __init__(self, _db):
                super().__init__(_db)
                self.considered = []
                self.processed = []

            def should_process_lane(self, lane):
                self.considered.append(lane)
                return lane.display_name == "process me"

            def process_lane(self, lane):
                self.processed.append(lane)

        good = db.lane(display_name="process me")
        bad = db.lane(display_name="don't process me")
        good_child = db.lane(display_name="process me", parent=bad)

        script = Mock(db.session)
        script.do_run(cmd_args=[])

        # The first item considered for processing was an ad hoc
        # WorkList representing the library's entire collection.
        worklist = script.considered.pop(0)
        assert db.default_library() == worklist.get_library(db.session)
        assert db.default_library().name == worklist.display_name
        assert {good, bad} == set(worklist.children)

        # After that, every lane was considered for processing, with
        # top-level lanes considered first.
        assert {good, bad, good_child} == set(script.considered)

        # But a lane was processed only if should_process_lane
        # returned True.
        assert {good, good_child} == set(script.processed)


class TestRunCoverageProviderScript:
    def test_parse_command_line(self, db: DatabaseTransactionFixture):
        identifier = db.identifier()
        cmd_args = [
            "--cutoff-time",
            "2016-05-01",
            "--identifier-type",
            identifier.type,
            identifier.identifier,
        ]
        parsed = RunCoverageProviderScript.parse_command_line(
            db.session, cmd_args, MockStdin()
        )
        assert datetime_utc(2016, 5, 1) == parsed.cutoff_time
        assert [identifier] == parsed.identifiers
        assert identifier.type == parsed.identifier_type


class TestRunThreadedCollectionCoverageProviderScript:
    def test_run(self, db: DatabaseTransactionFixture):
        provider = AlwaysSuccessfulCollectionCoverageProvider
        script = RunThreadedCollectionCoverageProviderScript(
            provider, worker_size=2, _db=db.session
        )

        # If there are no collections for the provider, run does nothing.
        # Pass a mock pool that will raise an error if it's used.
        pool = object()
        collection = db.collection(protocol=ExternalIntegration.ENKI)

        # Run exits without a problem because the pool is never touched.
        script.run(pool=pool)

        # Create some identifiers that need coverage.
        collection = db.collection()
        ed1, lp1 = db.edition(collection=collection, with_license_pool=True)
        ed2, lp2 = db.edition(collection=collection, with_license_pool=True)
        ed3 = db.edition()

        [id1, id2, id3] = [e.primary_identifier for e in (ed1, ed2, ed3)]

        # Set a timestamp for the provider.
        timestamp = Timestamp.stamp(
            db.session,
            provider.SERVICE_NAME,
            Timestamp.COVERAGE_PROVIDER_TYPE,
            collection=collection,
        )
        original_timestamp = timestamp.finish
        db.session.commit()

        pool = DatabasePool(2, script.session_factory)
        script.run(pool=pool)
        db.session.commit()

        # The expected number of workers and jobs have been created.
        assert 2 == len(pool.workers)
        assert 1 == pool.job_total

        # All relevant identifiers have been given coverage.
        source = DataSource.lookup(db.session, provider.DATA_SOURCE_NAME)
        identifiers_missing_coverage = Identifier.missing_coverage_from(
            db.session,
            provider.INPUT_IDENTIFIER_TYPES,
            source,
        )
        assert [id3] == identifiers_missing_coverage.all()

        record1, was_registered1 = provider.register(id1)
        record2, was_registered2 = provider.register(id2)
        assert CoverageRecord.SUCCESS == record1.status
        assert CoverageRecord.SUCCESS == record2.status
        assert (False, False) == (was_registered1, was_registered2)

        # The timestamp for the provider has been updated.
        new_timestamp = Timestamp.value(
            db.session,
            provider.SERVICE_NAME,
            Timestamp.COVERAGE_PROVIDER_TYPE,
            collection,
        )
        assert new_timestamp != original_timestamp
        assert new_timestamp > original_timestamp


class TestRunWorkCoverageProviderScript:
    def test_constructor(self, db: DatabaseTransactionFixture):
        script = RunWorkCoverageProviderScript(
            AlwaysSuccessfulWorkCoverageProvider, _db=db.session, batch_size=123
        )
        [provider] = script.providers
        assert isinstance(provider, AlwaysSuccessfulWorkCoverageProvider)
        assert 123 == provider.batch_size


class TestWorkProcessingScript:
    def test_make_query(self, db: DatabaseTransactionFixture):
        # Create two Gutenberg works and one Overdrive work
        g1 = db.work(with_license_pool=True, with_open_access_download=True)
        g2 = db.work(with_license_pool=True, with_open_access_download=True)

        overdrive_edition = db.edition(
            data_source_name=DataSource.OVERDRIVE,
            identifier_type=Identifier.OVERDRIVE_ID,
            with_license_pool=True,
        )[0]
        overdrive_work = db.work(presentation_edition=overdrive_edition)

        ugi_edition = db.edition(
            data_source_name=DataSource.UNGLUE_IT,
            identifier_type=Identifier.URI,
            with_license_pool=True,
        )[0]
        unglue_it = db.work(presentation_edition=ugi_edition)

        se_edition = db.edition(
            data_source_name=DataSource.STANDARD_EBOOKS,
            identifier_type=Identifier.URI,
            with_license_pool=True,
        )[0]
        standard_ebooks = db.work(presentation_edition=se_edition)

        everything = WorkProcessingScript.make_query(db.session, None, None, None)
        assert {g1, g2, overdrive_work, unglue_it, standard_ebooks} == set(
            everything.all()
        )

        all_gutenberg = WorkProcessingScript.make_query(
            db.session, Identifier.GUTENBERG_ID, [], None
        )
        assert {g1, g2} == set(all_gutenberg.all())

        one_gutenberg = WorkProcessingScript.make_query(
            db.session, Identifier.GUTENBERG_ID, [g1.license_pools[0].identifier], None
        )
        assert [g1] == one_gutenberg.all()

        one_standard_ebook = WorkProcessingScript.make_query(
            db.session, Identifier.URI, [], DataSource.STANDARD_EBOOKS
        )
        assert [standard_ebooks] == one_standard_ebook.all()


class TestTimestampInfo:

    TimestampInfo = DatabaseMigrationScript.TimestampInfo

    def test_find(self, db: DatabaseTransactionFixture):
        class Empty:
            _db: Session

        empty = Empty()
        empty._db = db.session

        # If there isn't a timestamp for the given service,
        # nothing is returned.
        result = self.TimestampInfo.find(empty, "test")
        assert None == result

        # But an empty Timestamp has been placed into the database.
        timestamp = (
            db.session.query(Timestamp).filter(Timestamp.service == "test").one()
        )
        assert None == timestamp.start
        assert None == timestamp.finish
        assert None == timestamp.counter

        # A repeat search for the empty Timestamp also results in None.
        script = DatabaseMigrationScript(db.session)
        assert None == self.TimestampInfo.find(script, "test")

        # If the Timestamp is stamped, it is returned.
        timestamp.finish = utc_now()
        timestamp.counter = 1
        db.session.flush()

        result = self.TimestampInfo.find(script, "test")
        assert timestamp.finish == result.finish
        assert 1 == result.counter

    def test_update(self, db: DatabaseTransactionFixture):
        # Create a Timestamp to be updated.
        past = strptime_utc("19980101", "%Y%m%d")
        stamp = Timestamp.stamp(
            db.session, "test", Timestamp.SCRIPT_TYPE, None, start=past, finish=past
        )
        script = DatabaseMigrationScript(db.session)
        timestamp_info = self.TimestampInfo.find(script, "test")

        now = utc_now()
        timestamp_info.update(db.session, now, 2)

        # When we refresh the Timestamp object, it's been updated.
        db.session.refresh(stamp)
        assert now == stamp.start
        assert now == stamp.finish
        assert 2 == stamp.counter

    def save(self, db: DatabaseTransactionFixture):
        # The Timestamp doesn't exist.
        timestamp_qu = db.session.query(Timestamp).filter(Timestamp.service == "test")
        assert False == timestamp_qu.exists()

        now = utc_now()
        timestamp_info = self.TimestampInfo("test", now, 47)
        timestamp_info.save(db.session)

        # The Timestamp exists now.
        timestamp = timestamp_qu.one()
        assert now == timestamp.finish
        assert 47 == timestamp.counter


@pytest.fixture
def migration_dirs(tmp_path):
    # create migration file structure
    server = tmp_path / "migration"
    core = tmp_path / "server_core" / "migation"
    server.mkdir()
    core.mkdir(parents=True)

    # return fixture
    yield [str(core), str(server)]

    # cleanup files
    def recursive_delete(path):
        for file in path.iterdir():
            if file.is_file():
                file.unlink()
            if file.is_dir():
                recursive_delete(file)
                file.rmdir()

    recursive_delete(tmp_path)


@pytest.fixture()
def migration_file(tmp_path):
    def create_migration_file(
        directory, unique_string, migration_type, migration_date=None
    ):
        suffix = "." + migration_type

        if migration_type == "sql":
            # Create unique, innocuous content for a SQL file.
            # This SQL inserts a timestamp into the test database.
            service = "Test Database Migration Script - %s" % unique_string
            content = (
                "insert into timestamps(service, finish)" " values ('%s', '%s');"
            ) % (service, "1970-01-01")
        elif migration_type == "py":
            # Create unique, innocuous content for a Python file.
            content = (
                "#!/usr/bin/env python\n\n"
                + "import tempfile\nimport os\n\n"
                + "file_info = tempfile.mkstemp(prefix='"
                + unique_string
                + "-', suffix='.py', dir='"
                + str(tmp_path)
                + "')\n\n"
                + "# Close file descriptor\n"
                + "os.close(file_info[0])\n"
            )
        else:
            content = ""

        if not migration_date:
            # Default date is just after self.timestamp.
            migration_date = "20260811"
        prefix = migration_date + "-"

        fd, migration_file = tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=directory, text=True
        )
        os.write(fd, content.encode("utf-8"))

        # If it's a python migration, make it executable.
        if migration_file.endswith("py"):
            original_mode = os.stat(migration_file).st_mode
            mode = original_mode | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.chmod(migration_file, mode)

        # Close the file descriptor.
        os.close(fd)

        # return the filename
        return migration_file

    return create_migration_file


@pytest.fixture()
def migrations(migration_file, migration_dirs):
    # Put a file of each migration type in each temporary migration directory.
    core_migration_files = []
    server_migration_files = []
    [core_dir, server_dir] = migration_dirs
    core_migration_files.append(migration_file(core_dir, "CORE", "sql"))
    core_migration_files.append(migration_file(core_dir, "CORE", "py"))
    server_migration_files.append(migration_file(server_dir, "SERVER", "sql"))
    server_migration_files.append(migration_file(server_dir, "SERVER", "py"))
    return core_migration_files, server_migration_files


class DatabaseMigrationScriptFixture:
    """A fixture used for database migration scripts. Ensures the use of custom migration directories,
    and cleans up the database afterwards."""

    db: DatabaseTransactionFixture
    script: DatabaseMigrationScript
    migration_dirs: list[str]
    migrations: tuple[list[str], list[str]]

    def close(self):
        self.db.session.query(Timestamp).filter(
            Timestamp.service.like("%Database Migration%")
        ).delete(synchronize_session=False)


class DatabaseMigrationInitializationScriptFixture:
    """A fixture used for database migration scripts. Ensures the use of custom migration directories,
    and cleans up the database afterwards."""

    db: DatabaseTransactionFixture
    script: DatabaseMigrationScript
    migration_dirs: list[str]
    migrations: tuple[list[str], list[str]]

    def close(self):
        self.db.session.query(Timestamp).filter(
            Timestamp.service.like("%Database Migration%")
        ).delete(synchronize_session=False)


@pytest.fixture()
def database_migration_script_fixture(
    db: DatabaseTransactionFixture,
    monkeypatch,
    migrations: tuple[list[str], list[str]],
    migration_dirs: list[str],
) -> Iterable[DatabaseMigrationScriptFixture]:
    # Patch DatabaseMigrationScript to use test directories for migrations
    monkeypatch.setattr(
        DatabaseMigrationScript, "directories_by_priority", migration_dirs
    )

    fixture = DatabaseMigrationScriptFixture()
    fixture.migration_dirs = migration_dirs
    fixture.migrations = migrations
    fixture.script = DatabaseMigrationScript(db.session)
    fixture.db = db
    yield fixture
    fixture.close()


@pytest.fixture()
def database_migration_initialization_script_fixture(
    db: DatabaseTransactionFixture,
    monkeypatch,
    migrations: tuple[list[str], list[str]],
    migration_dirs: list[str],
) -> Iterable[DatabaseMigrationInitializationScriptFixture]:
    # Patch DatabaseMigrationInitializationScript to use test directories for migrations
    monkeypatch.setattr(
        DatabaseMigrationInitializationScript, "directories_by_priority", migration_dirs
    )

    fixture = DatabaseMigrationInitializationScriptFixture()
    fixture.migration_dirs = migration_dirs
    fixture.migrations = migrations
    fixture.script = DatabaseMigrationInitializationScript(db.session)
    fixture.db = db
    yield fixture
    fixture.close()


class TestDatabaseMigrationScript:
    @pytest.fixture()
    def timestamp(
        self, database_migration_script_fixture: DatabaseMigrationScriptFixture
    ):
        fixture = database_migration_script_fixture
        session = fixture.db.session
        script = fixture.script

        stamp = strptime_utc("20260810", "%Y%m%d")
        timestamp = Timestamp(service=script.name, start=stamp, finish=stamp)
        python_timestamp = Timestamp(
            service=script.PY_TIMESTAMP_SERVICE_NAME, start=stamp, finish=stamp
        )
        session.add_all([timestamp, python_timestamp])
        session.flush()

        timestamp_info = script.TimestampInfo(timestamp.service, timestamp.start)
        return timestamp, python_timestamp, timestamp_info

    def test_name(
        self, database_migration_script_fixture: DatabaseMigrationScriptFixture
    ):
        """DatabaseMigrationScript.name returns an appropriate timestamp service
        name, depending on whether it is running only Python migrations or not.
        """

        script = database_migration_script_fixture.script
        # The default script returns the default timestamp name.
        assert "Database Migration" == script.name

        # A python-only script returns a Python-specific timestamp name.
        script.python_only = True
        assert "Database Migration - Python" == script.name

    def test_timestamp_properties(
        self, database_migration_script_fixture: DatabaseMigrationScriptFixture
    ):
        """DatabaseMigrationScript provides the appropriate TimestampInfo
        objects as properties.
        """
        script = database_migration_script_fixture.script
        transaction, session = (
            database_migration_script_fixture.db,
            database_migration_script_fixture.db.session,
        )

        # If there aren't any Database Migrations in the database, no
        # timestamps are returned.
        timestamps = session.query(Timestamp).filter(
            Timestamp.service.like("Database Migration%")
        )
        for timestamp in timestamps:
            session.delete(timestamp)
        session.commit()

        script._session = session
        assert None == script.python_timestamp
        assert None == script.overall_timestamp

        # If the Timestamps exist in the database, but they don't have
        # a timestamp, nothing is returned. Timestamps must be initialized.
        overall = (
            session.query(Timestamp)
            .filter(Timestamp.service == script.SERVICE_NAME)
            .one()
        )
        python = (
            session.query(Timestamp)
            .filter(Timestamp.service == script.PY_TIMESTAMP_SERVICE_NAME)
            .one()
        )

        # Neither Timestamp object has a timestamp.
        assert (None, None) == (python.finish, overall.finish)
        # So neither timestamp is returned as a property.
        assert None == script.python_timestamp
        assert None == script.overall_timestamp

        # If you give the Timestamps data, suddenly they show up.
        overall.finish = script.parse_time("1998-08-25")
        python.finish = script.parse_time("1993-06-11")
        python.counter = 2
        session.flush()

        overall_timestamp_info = script.overall_timestamp
        assert isinstance(overall_timestamp_info, script.TimestampInfo)
        assert overall.finish == overall_timestamp_info.finish

        python_timestamp_info = script.python_timestamp
        assert isinstance(python_timestamp_info, script.TimestampInfo)
        assert python.finish == python_timestamp_info.finish
        assert 2 == script.python_timestamp.counter

    def test_directories_by_priority(self):
        root = Path(__file__).parent.parent.parent
        expected_core = root / "core" / "migration"
        expected_parent = root / "migration"

        # This is the only place we're testing the real script.
        # Everywhere else should use the mock.
        script = DatabaseMigrationScript()
        assert [
            str(expected_core),
            str(expected_parent),
        ] == script.directories_by_priority

    def test_fetch_migration_files(
        self, database_migration_script_fixture: DatabaseMigrationScriptFixture
    ):
        script = database_migration_script_fixture.script
        migration_dirs = database_migration_script_fixture.migration_dirs
        migrations = database_migration_script_fixture.migrations

        result = script.fetch_migration_files()
        result_migrations, result_migrations_by_dir = result
        core_migrations, server_migrations = migrations
        all_migrations = []
        all_migrations.extend(core_migrations)
        all_migrations.extend(server_migrations)
        [core_migration_dir, server_migration_dir] = migration_dirs

        for migration_file in all_migrations:
            assert os.path.split(migration_file)[1] in result_migrations

        # Ensure that all the expected migrations from CORE are included in
        # the 'core' directory array in migrations_by_directory.
        assert 2 == len(core_migrations)
        for filename in core_migrations:
            assert (
                os.path.split(filename)[1]
                in result_migrations_by_dir[core_migration_dir]
            )

        # Ensure that all the expected migrations from the parent server
        # are included in the appropriate array in migrations_by_directory.
        assert 2 == len(server_migrations)
        for filename in server_migrations:
            assert (
                os.path.split(filename)[1]
                in result_migrations_by_dir[server_migration_dir]
            )

        # When the script is python_only, only python migrations are returned.
        script.python_only = True
        result_migrations, result_migrations_by_dir = script.fetch_migration_files()

        py_migration_files = [m for m in all_migrations if m.endswith(".py")]
        py_migration_filenames = [os.path.split(f)[1] for f in py_migration_files]
        assert sorted(py_migration_filenames) == sorted(result_migrations)

        core_migration_files = [
            os.path.split(m)[1] for m in core_migrations if m.endswith(".py")
        ]
        assert 1 == len(core_migration_files)
        assert result_migrations_by_dir[core_migration_dir] == core_migration_files

        server_migration_files = [
            os.path.split(m)[1] for m in server_migrations if m.endswith(".py")
        ]
        assert 1 == len(server_migration_files)
        assert result_migrations_by_dir[server_migration_dir] == server_migration_files

    def test_migratable_files(
        self, database_migration_script_fixture: DatabaseMigrationScriptFixture
    ):
        """Returns migrations that end with particular extensions."""
        script = database_migration_script_fixture.script

        migrations = [
            ".gitkeep",
            "20250521-make-bananas.sql",
            "20260810-do-a-thing.py",
            "20260802-did-a-thing.pyc",
            "why-am-i-here.rb",
        ]

        result = script.migratable_files(migrations, [".sql", ".py"])
        assert 2 == len(result)
        assert ["20250521-make-bananas.sql", "20260810-do-a-thing.py"] == result

        result = script.migratable_files(migrations, [".rb"])
        assert 1 == len(result)
        assert ["why-am-i-here.rb"] == result

        result = script.migratable_files(migrations, ["banana"])
        assert [] == result

    def test_get_new_migrations(
        self,
        database_migration_script_fixture: DatabaseMigrationScriptFixture,
        timestamp,
    ):
        """Filters out migrations that were run on or before a given timestamp"""
        script = database_migration_script_fixture.script
        timestamp, python_timestamp, timestamp_info = timestamp

        migrations = [
            "20271204-far-future-migration-funtime.sql",
            "20271202-future-migration-funtime.sql",
            "20271203-do-another-thing.py",
            "20250521-make-bananas.sql",
            "20260810-last-timestamp",
            "20260811-do-a-thing.py",
            "20260809-already-done.sql",
        ]

        result = script.get_new_migrations(timestamp_info, migrations)
        # Expected migrations will be sorted by timestamp. Python migrations
        # will be sorted after SQL migrations.
        expected = [
            "20271202-future-migration-funtime.sql",
            "20271204-far-future-migration-funtime.sql",
            "20260811-do-a-thing.py",
            "20271203-do-another-thing.py",
        ]

        assert 4 == len(result)
        assert expected == result

        # If the timestamp has a counter, the filter only finds new migrations
        # past the counter.
        migrations = [
            "20260810-last-timestamp.sql",
            "20260810-1-do-a-thing.sql",
            "20271202-future-migration-funtime.sql",
            "20260810-2-do-all-the-things.sql",
            "20260809-already-done.sql",
        ]
        timestamp_info.counter = 1
        result = script.get_new_migrations(timestamp_info, migrations)
        expected = [
            "20260810-2-do-all-the-things.sql",
            "20271202-future-migration-funtime.sql",
        ]

        assert 2 == len(result)
        assert expected == result

        # If the timestamp has a (unlikely) mix of counter and non-counter
        # migrations with the same datetime, migrations with counters are
        # sorted after migrations without them.
        migrations = [
            "20260810-do-a-thing.sql",
            "20271202-1-more-future-migration-funtime.sql",
            "20260810-1-do-all-the-things.sql",
            "20260809-already-done.sql",
            "20271202-future-migration-funtime.sql",
        ]
        timestamp_info.counter = None

        result = script.get_new_migrations(timestamp_info, migrations)
        expected = [
            "20260810-1-do-all-the-things.sql",
            "20271202-future-migration-funtime.sql",
            "20271202-1-more-future-migration-funtime.sql",
        ]
        assert 3 == len(result)
        assert expected == result

    def test_update_timestamps(
        self,
        database_migration_script_fixture: DatabaseMigrationScriptFixture,
        timestamp,
    ):
        """Resets a timestamp according to the date of a migration file"""
        fixture = database_migration_script_fixture
        script = fixture.script
        migration_dirs = fixture.migration_dirs
        transaction, session = fixture.db, fixture.db.session
        timestamp, python_timestamp, timestamp_info = timestamp

        migration = "20271202-future-migration-funtime.sql"
        py_last_run_time = python_timestamp.finish

        def assert_unchanged_python_timestamp():
            assert py_last_run_time == python_timestamp.finish

        def assert_timestamp_matches_migration(timestamp, migration, counter=None):
            session.refresh(timestamp)
            timestamp_str = timestamp.finish.strftime("%Y%m%d")
            assert migration[0:8] == timestamp_str
            assert counter == timestamp.counter

        assert timestamp_info.finish.strftime("%Y%m%d") != migration[0:8]
        script.update_timestamps(migration)
        assert_timestamp_matches_migration(timestamp, migration)
        assert_unchanged_python_timestamp()

        # It also takes care of counter digits when multiple migrations
        # exist for the same date.
        migration = "20280810-2-do-all-the-things.sql"
        script.update_timestamps(migration)
        assert_timestamp_matches_migration(timestamp, migration, counter=2)
        assert_unchanged_python_timestamp()

        # And removes those counter digits when the timestamp is updated.
        migration = "20280901-what-it-do.sql"
        script.update_timestamps(migration)
        assert_timestamp_matches_migration(timestamp, migration)
        assert_unchanged_python_timestamp()

        # If the migration is earlier than the existing timestamp,
        # the timestamp is not updated.
        migration = "20280801-before-the-existing-timestamp.sql"
        script.update_timestamps(migration)
        assert timestamp.finish.strftime("%Y%m%d") == "20280901"

        # Python migrations update both timestamps.
        migration = "20281001-new-task.py"
        script.update_timestamps(migration)
        assert_timestamp_matches_migration(timestamp, migration)
        assert_timestamp_matches_migration(python_timestamp, migration)

    def test_running_a_migration_updates_the_timestamps(
        self,
        database_migration_script_fixture: DatabaseMigrationScriptFixture,
        timestamp,
        migration_file,
    ):
        fixture = database_migration_script_fixture
        script = fixture.script

        timestamp, python_timestamp, timestamp_info = timestamp
        future_time = strptime_utc("20261030", "%Y%m%d")
        timestamp_info.finish = future_time
        [core_dir, server_dir] = fixture.migration_dirs

        # Create a test migration after that point and grab relevant info about it.
        migration_filepath = migration_file(
            core_dir, "SINGLE", "sql", migration_date="20261202"
        )

        # Run the migration with the relevant information.
        migration_filename = os.path.split(migration_filepath)[1]
        migrations_by_dir = {core_dir: [migration_filename], server_dir: []}

        # Running the migration updates the timestamps
        script.run_migrations([migration_filename], migrations_by_dir, timestamp_info)
        assert timestamp.finish.strftime("%Y%m%d") == "20261202"

        # Even when there are counters.
        migration_filepath = migration_file(
            core_dir, "COUNTER", "sql", migration_date="20261203-3"
        )
        migration_filename = os.path.split(migration_filepath)[1]
        migrations_by_dir[core_dir] = [migration_filename]
        script.run_migrations([migration_filename], migrations_by_dir, timestamp_info)
        assert timestamp.finish.strftime("%Y%m%d") == "20261203"
        assert timestamp.counter == 3

    def test_all_migration_files_are_run(
        self,
        database_migration_script_fixture: DatabaseMigrationScriptFixture,
        timestamp,
        tmp_path,
    ):
        fixture = database_migration_script_fixture
        script = fixture.script
        transaction, session = fixture.db, fixture.db.session

        script.run(
            test_db=session, test=True, cmd_args=["--last-run-date", "2010-01-01"]
        )

        # There are two test timestamps in the database, confirming that
        # the test SQL files created by the migrations fixture
        # have been run.
        timestamps = (
            session.query(Timestamp)
            .filter(Timestamp.service.like("Test Database Migration Script - %"))
            .order_by(Timestamp.service)
            .all()
        )
        assert 2 == len(timestamps)

        # A timestamp has been generated from each migration directory.
        assert True == timestamps[0].service.endswith("CORE")
        assert True == timestamps[1].service.endswith("SERVER")

        for timestamp in timestamps:
            session.delete(timestamp)

        # There are two temporary files created in tmp_path,
        # confirming that the test Python files created by
        # migrations fixture have been run.
        test_generated_files = sorted(
            f.name
            for f in tmp_path.iterdir()
            if f.name.startswith(("CORE", "SERVER")) and f.is_file()
        )
        assert 2 == len(test_generated_files)

        # A file has been generated from each migration directory.
        assert "CORE" in test_generated_files[0]
        assert "SERVER" in test_generated_files[1]

    def test_python_migration_files_can_be_run_independently(
        self,
        database_migration_script_fixture: DatabaseMigrationScriptFixture,
        timestamp,
        tmp_path,
    ):
        fixture = database_migration_script_fixture
        script = fixture.script
        transaction, session = fixture.db, fixture.db.session

        script.run(
            test_db=session,
            test=True,
            cmd_args=["--last-run-date", "2010-01-01", "--python-only"],
        )

        # There are no test timestamps in the database, confirming that
        # no test SQL files created by the migrations fixture
        # have been run.
        timestamps = (
            session.query(Timestamp)
            .filter(Timestamp.service.like("Test Database Migration Script - %"))
            .order_by(Timestamp.service)
            .all()
        )
        assert [] == timestamps

        # There are two temporary files in tmp_path, confirming that the test
        # Python files created by the migrations fixture were run.
        test_dir = os.path.split(__file__)[0]
        all_files = os.listdir(test_dir)
        test_generated_files = sorted(
            f.name
            for f in tmp_path.iterdir()
            if f.name.startswith(("CORE", "SERVER")) and f.is_file()
        )

        assert 2 == len(test_generated_files)

        # A file has been generated from each migration directory.
        assert "CORE" in test_generated_files[0]
        assert "SERVER" in test_generated_files[1]


class TestDatabaseMigrationInitializationScript:
    def assert_matches_latest_python_migration(
        self,
        timestamp,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
    ):
        script = database_migration_initialization_script_fixture.script
        migrations = script.fetch_migration_files()[0]
        migrations_sorted = script.sort_migrations(migrations)
        last_migration_date = [x for x in migrations_sorted if x.endswith(".py")][-1][
            0:8
        ]
        self.assert_matches_timestamp(timestamp, last_migration_date)

    def assert_matches_latest_migration(
        self,
        timestamp,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
    ):
        script = database_migration_initialization_script_fixture.script
        migrations = script.fetch_migration_files()[0]
        migrations_sorted = script.sort_migrations(migrations)
        py_migration = [x for x in migrations_sorted if x.endswith(".py")][-1][0:8]
        sql_migration = [x for x in migrations_sorted if x.endswith(".sql")][-1][0:8]
        last_migration_date = (
            py_migration if int(py_migration) > int(sql_migration) else sql_migration
        )
        self.assert_matches_timestamp(timestamp, last_migration_date)

    @staticmethod
    def assert_matches_timestamp(timestamp, migration_date):
        assert timestamp.finish.strftime("%Y%m%d") == migration_date

    def test_accurate_timestamps_created(
        self,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
    ):
        fixture = database_migration_initialization_script_fixture
        script = fixture.script
        db, session = fixture.db, fixture.db.session

        assert None == Timestamp.value(
            session, script.name, Timestamp.SCRIPT_TYPE, collection=None
        )
        script.run()
        self.assert_matches_latest_migration(script.overall_timestamp, fixture)
        self.assert_matches_latest_python_migration(script.python_timestamp, fixture)

    def test_accurate_python_timestamp_created_python_later(
        self,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
        migration_file,
    ):
        fixture = database_migration_initialization_script_fixture
        script = fixture.script
        db, session = fixture.db, fixture.db.session

        [core_migration_dir, server_migration_dir] = fixture.migration_dirs
        assert None == Timestamp.value(
            session, script.name, Timestamp.SCRIPT_TYPE, collection=None
        )

        # If the last python migration and the last SQL migration have
        # different timestamps, they're set accordingly.
        migration_file(core_migration_dir, "CORE", "sql", "20310101")
        migration_file(server_migration_dir, "SERVER", "py", "20300101")

        script.run()
        self.assert_matches_timestamp(script.overall_timestamp, "20310101")
        self.assert_matches_timestamp(script.python_timestamp, "20300101")

    def test_accurate_python_timestamp_created_python_earlier(
        self,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
        migration_file,
    ):
        fixture = database_migration_initialization_script_fixture
        script, migration_dirs = fixture.script, fixture.migration_dirs
        db, session = fixture.db, fixture.db.session

        [core_migration_dir, server_migration_dir] = migration_dirs
        assert None == Timestamp.value(
            session, script.name, Timestamp.SCRIPT_TYPE, collection=None
        )

        # If the last python migration and the last SQL migration have
        # different timestamps, they're set accordingly.
        migration_file(core_migration_dir, "CORE", "sql", "20310101")
        migration_file(server_migration_dir, "SERVER", "py", "20350101")

        script.run()
        self.assert_matches_timestamp(script.overall_timestamp, "20350101")
        self.assert_matches_timestamp(script.python_timestamp, "20350101")

    def test_error_raised_when_timestamp_exists(self, db: DatabaseTransactionFixture):
        session = db.session

        script = DatabaseMigrationInitializationScript(session)
        Timestamp.stamp(session, script.name, Timestamp.SCRIPT_TYPE, None)
        pytest.raises(RuntimeError, script.run)

    def test_error_not_raised_when_timestamp_forced(
        self,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
    ):
        fixture = database_migration_initialization_script_fixture
        script, migration_dirs = fixture.script, fixture.migration_dirs
        db, session = fixture.db, fixture.db.session

        past = script.parse_time("19951127")
        Timestamp.stamp(session, script.name, Timestamp.SCRIPT_TYPE, None, finish=past)
        script.run(["-f"])
        self.assert_matches_latest_migration(script.overall_timestamp, fixture)
        self.assert_matches_latest_python_migration(script.python_timestamp, fixture)

    def test_accepts_last_run_date(
        self,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
    ):
        script = database_migration_initialization_script_fixture.script
        # A timestamp can be passed via the command line.
        script.run(["--last-run-date", "20101010"])
        expected_stamp = strptime_utc("20101010", "%Y%m%d")
        assert expected_stamp == script.overall_timestamp.finish

        # It will override an existing timestamp if forced.
        script.run(["--last-run-date", "20111111", "--force"])
        expected_stamp = strptime_utc("20111111", "%Y%m%d")
        assert expected_stamp == script.overall_timestamp.finish
        assert expected_stamp == script.python_timestamp.finish

    def test_accepts_last_run_counter(
        self,
        database_migration_initialization_script_fixture: DatabaseMigrationInitializationScriptFixture,
    ):
        script = database_migration_initialization_script_fixture.script
        # If a counter is passed without a date, an error is raised.
        pytest.raises(ValueError, script.run, ["--last-run-counter", "7"])

        # With a date, the counter can be set.
        script.run(["--last-run-date", "20101010", "--last-run-counter", "7"])
        expected_stamp = strptime_utc("20101010", "%Y%m%d")
        assert expected_stamp == script.overall_timestamp.finish
        assert 7 == script.overall_timestamp.counter

        # When forced, the counter can be reset on an existing timestamp.
        previous_timestamp = script.overall_timestamp.finish
        script.run(["--last-run-date", "20121212", "--last-run-counter", "2", "-f"])
        expected_stamp = strptime_utc("20121212", "%Y%m%d")
        assert expected_stamp == script.overall_timestamp.finish
        assert expected_stamp == script.python_timestamp.finish
        assert 2 == script.overall_timestamp.counter
        assert 2 == script.python_timestamp.counter


class TestAddClassificationScript:
    def test_end_to_end(self, db: DatabaseTransactionFixture):
        work = db.work(with_license_pool=True)
        identifier = work.license_pools[0].identifier
        stdin = MockStdin(identifier.identifier)
        assert Classifier.AUDIENCE_ADULT == work.audience

        cmd_args = [
            "--identifier-type",
            identifier.type,
            "--subject-type",
            Classifier.FREEFORM_AUDIENCE,
            "--subject-identifier",
            Classifier.AUDIENCE_CHILDREN,
            "--weight",
            "42",
            "--create-subject",
        ]
        script = AddClassificationScript(_db=db.session, cmd_args=cmd_args, stdin=stdin)
        script.do_run()

        # The identifier has been classified under 'children'.
        [classification] = identifier.classifications
        assert 42 == classification.weight
        subject = classification.subject
        assert Classifier.FREEFORM_AUDIENCE == subject.type
        assert Classifier.AUDIENCE_CHILDREN == subject.identifier

        # The work has been reclassified and is now known as a
        # children's book.
        assert Classifier.AUDIENCE_CHILDREN == work.audience

    def test_autocreate(self, db: DatabaseTransactionFixture):
        work = db.work(with_license_pool=True)
        identifier = work.license_pools[0].identifier
        stdin = MockStdin(identifier.identifier)
        assert Classifier.AUDIENCE_ADULT == work.audience

        cmd_args = [
            "--identifier-type",
            identifier.type,
            "--subject-type",
            Classifier.TAG,
            "--subject-identifier",
            "some random tag",
        ]
        script = AddClassificationScript(_db=db.session, cmd_args=cmd_args, stdin=stdin)
        script.do_run()

        # Nothing has happened. There was no Subject with that
        # identifier, so we assumed there was a typo and did nothing.
        assert [] == identifier.classifications

        # If we stick the 'create-subject' onto the end of the
        # command-line arguments, the Subject is created and the
        # classification happens.
        stdin = MockStdin(identifier.identifier)
        cmd_args.append("--create-subject")
        script = AddClassificationScript(_db=db.session, cmd_args=cmd_args, stdin=stdin)
        script.do_run()

        [classification] = identifier.classifications
        subject = classification.subject
        assert "some random tag" == subject.identifier


class TestShowLibrariesScript:
    def test_with_no_libraries(self, db: DatabaseTransactionFixture):
        output = StringIO()
        ShowLibrariesScript().do_run(db.session, output=output)
        assert "No libraries found.\n" == output.getvalue()

    def test_with_multiple_libraries(self, db: DatabaseTransactionFixture):
        l1, ignore = create(
            db.session,
            Library,
            name="Library 1",
            short_name="L1",
        )
        l1.library_registry_shared_secret = "a"
        l2, ignore = create(
            db.session,
            Library,
            name="Library 2",
            short_name="L2",
        )
        l2.library_registry_shared_secret = "b"

        # The output of this script is the result of running explain()
        # on both libraries.
        output = StringIO()
        ShowLibrariesScript().do_run(db.session, output=output)
        expect_1 = "\n".join(l1.explain(include_secrets=False))
        expect_2 = "\n".join(l2.explain(include_secrets=False))

        assert expect_1 + "\n" + expect_2 + "\n" == output.getvalue()

        # We can tell the script to only list a single library.
        output = StringIO()
        ShowLibrariesScript().do_run(
            db.session, cmd_args=["--short-name=L2"], output=output
        )
        assert expect_2 + "\n" == output.getvalue()

        # We can tell the script to include the library registry
        # shared secret.
        output = StringIO()
        ShowLibrariesScript().do_run(
            db.session, cmd_args=["--show-secrets"], output=output
        )
        expect_1 = "\n".join(l1.explain(include_secrets=True))
        expect_2 = "\n".join(l2.explain(include_secrets=True))
        assert expect_1 + "\n" + expect_2 + "\n" == output.getvalue()


class TestConfigureSiteScript:
    def test_unknown_setting(self, db: DatabaseTransactionFixture):
        script = ConfigureSiteScript()
        with pytest.raises(ValueError) as excinfo:
            script.do_run(db.session, ["--setting=setting1=value1"])
        assert (
            "'setting1' is not a known site-wide setting. Use --force to set it anyway."
            in str(excinfo.value)
        )

        assert None == ConfigurationSetting.sitewide(db.session, "setting1").value

        # Running with --force sets the setting.
        script.do_run(
            db.session,
            [
                "--setting=setting1=value1",
                "--force",
            ],
        )

        assert "value1" == ConfigurationSetting.sitewide(db.session, "setting1").value

    def test_settings(self, db: DatabaseTransactionFixture):
        class TestConfig:
            SITEWIDE_SETTINGS = [
                {"key": "setting1"},
                {"key": "setting2"},
                {"key": "setting_secret"},
            ]

        script = ConfigureSiteScript(config=TestConfig)
        output = StringIO()
        script.do_run(
            db.session,
            [
                "--setting=setting1=value1",
                '--setting=setting2=[1,2,"3"]',
                "--setting=setting_secret=secretvalue",
            ],
            output,
        )
        # The secret was set, but is not shown.
        expect = "\n".join(
            ConfigurationSetting.explain(db.session, include_secrets=False)
        )
        assert expect == output.getvalue()
        assert "setting_secret" not in expect
        assert "value1" == ConfigurationSetting.sitewide(db.session, "setting1").value
        assert (
            '[1,2,"3"]' == ConfigurationSetting.sitewide(db.session, "setting2").value
        )
        assert (
            "secretvalue"
            == ConfigurationSetting.sitewide(db.session, "setting_secret").value
        )

        # If we run again with --show-secrets, the secret is shown.
        output = StringIO()
        script.do_run(db.session, ["--show-secrets"], output)
        expect = "\n".join(
            ConfigurationSetting.explain(db.session, include_secrets=True)
        )
        assert expect == output.getvalue()
        assert "setting_secret" in expect


class TestConfigureLibraryScript:
    def test_bad_arguments(self, db: DatabaseTransactionFixture):
        script = ConfigureLibraryScript()
        library, ignore = create(
            db.session,
            Library,
            name="Library 1",
            short_name="L1",
        )
        library.library_registry_shared_secret = "secret"
        db.session.commit()
        with pytest.raises(ValueError) as excinfo:
            script.do_run(db.session, [])
        assert "You must identify the library by its short name." in str(excinfo.value)

        with pytest.raises(ValueError) as excinfo:
            script.do_run(db.session, ["--short-name=foo"])
        assert "Could not locate library 'foo'" in str(excinfo.value)

    def test_create_library(self, db: DatabaseTransactionFixture):
        # There is no library.
        assert [] == db.session.query(Library).all()

        script = ConfigureLibraryScript()
        output = StringIO()
        script.do_run(
            db.session,
            [
                "--short-name=L1",
                "--name=Library 1",
                "--setting=customkey=value",
            ],
            output,
        )

        # Now there is one library.
        [library] = db.session.query(Library).all()
        assert "Library 1" == library.name
        assert "L1" == library.short_name
        assert "value" == library.setting("customkey").value
        expect_output = (
            "Configuration settings stored.\n" + "\n".join(library.explain()) + "\n"
        )
        assert expect_output == output.getvalue()

    def test_reconfigure_library(self, db: DatabaseTransactionFixture):
        # The library exists.
        library, ignore = create(
            db.session,
            Library,
            name="Library 1",
            short_name="L1",
        )
        script = ConfigureLibraryScript()
        output = StringIO()

        # We're going to change one value and add a setting.
        script.do_run(
            db.session,
            [
                "--short-name=L1",
                "--name=Library 1 New Name",
                "--setting=customkey=value",
            ],
            output,
        )

        assert "Library 1 New Name" == library.name
        assert "value" == library.setting("customkey").value

        expect_output = (
            "Configuration settings stored.\n" + "\n".join(library.explain()) + "\n"
        )
        assert expect_output == output.getvalue()


class TestShowCollectionsScript:
    def test_with_no_collections(self, db: DatabaseTransactionFixture):
        output = StringIO()
        ShowCollectionsScript().do_run(db.session, output=output)
        assert "No collections found.\n" == output.getvalue()

    def test_with_multiple_collections(self, db: DatabaseTransactionFixture):
        c1 = db.collection(name="Collection 1", protocol=ExternalIntegration.OVERDRIVE)
        c1.collection_password = "a"
        c2 = db.collection(
            name="Collection 2", protocol=ExternalIntegration.BIBLIOTHECA
        )
        c2.collection_password = "b"

        # The output of this script is the result of running explain()
        # on both collections.
        output = StringIO()
        ShowCollectionsScript().do_run(db.session, output=output)
        expect_1 = "\n".join(c1.explain(include_secrets=False))
        expect_2 = "\n".join(c2.explain(include_secrets=False))

        assert expect_1 + "\n" + expect_2 + "\n" == output.getvalue()

        # We can tell the script to only list a single collection.
        output = StringIO()
        ShowCollectionsScript().do_run(
            db.session, cmd_args=["--name=Collection 2"], output=output
        )
        assert expect_2 + "\n" == output.getvalue()

        # We can tell the script to include the collection password
        output = StringIO()
        ShowCollectionsScript().do_run(
            db.session, cmd_args=["--show-secrets"], output=output
        )
        expect_1 = "\n".join(c1.explain(include_secrets=True))
        expect_2 = "\n".join(c2.explain(include_secrets=True))
        assert expect_1 + "\n" + expect_2 + "\n" == output.getvalue()


class TestConfigureCollectionScript:
    def test_bad_arguments(self, db: DatabaseTransactionFixture):
        script = ConfigureCollectionScript()
        library, ignore = create(
            db.session,
            Library,
            name="Library 1",
            short_name="L1",
        )
        db.session.commit()

        # Reference to a nonexistent collection without the information
        # necessary to create it.
        with pytest.raises(ValueError) as excinfo:
            script.do_run(db.session, ["--name=collection"])
        assert (
            'No collection called "collection". You can create it, but you must specify a protocol.'
            in str(excinfo.value)
        )

        # Incorrect format for the 'setting' argument.
        with pytest.raises(ValueError) as excinfo:
            script.do_run(
                db.session,
                ["--name=collection", "--protocol=Overdrive", "--setting=key"],
            )
        assert 'Incorrect format for setting: "key". Should be "key=value"' in str(
            excinfo.value
        )

        # Try to add the collection to a nonexistent library.
        with pytest.raises(ValueError) as excinfo:
            script.do_run(
                db.session,
                [
                    "--name=collection",
                    "--protocol=Overdrive",
                    "--library=nosuchlibrary",
                ],
            )
        assert 'No such library: "nosuchlibrary". I only know about: "L1"' in str(
            excinfo.value
        )

    def test_success(self, db: DatabaseTransactionFixture):
        script = ConfigureCollectionScript()
        l1, ignore = create(
            db.session,
            Library,
            name="Library 1",
            short_name="L1",
        )
        l2, ignore = create(
            db.session,
            Library,
            name="Library 2",
            short_name="L2",
        )
        l3, ignore = create(
            db.session,
            Library,
            name="Library 3",
            short_name="L3",
        )
        db.session.commit()

        # Create a collection, set all its attributes, set a custom
        # setting, and associate it with two libraries.
        output = StringIO()
        script.do_run(
            db.session,
            [
                "--name=New Collection",
                "--protocol=Overdrive",
                "--library=L2",
                "--library=L1",
                "--setting=library_id=1234",
                "--external-account-id=acctid",
                "--url=url",
                "--username=username",
                "--password=password",
            ],
            output,
        )

        # The collection was created and configured properly.
        collection = get_one(db.session, Collection)
        assert "New Collection" == collection.name
        assert "url" == collection.external_integration.url
        assert "acctid" == collection.external_account_id
        assert "username" == collection.external_integration.username
        assert "password" == collection.external_integration.password

        # Two libraries now have access to the collection.
        assert [collection] == l1.collections
        assert [collection] == l2.collections
        assert [] == l3.collections

        # One CollectionSetting was set on the collection, in addition
        # to url, username, and password.
        setting = collection.external_integration.setting("library_id")
        assert "library_id" == setting.key
        assert "1234" == setting.value

        # The output explains the collection settings.
        expect = (
            "Configuration settings stored.\n" + "\n".join(collection.explain()) + "\n"
        )
        assert expect == output.getvalue()

    def test_reconfigure_collection(self, db: DatabaseTransactionFixture):
        # The collection exists.
        collection = db.collection(
            name="Collection 1", protocol=ExternalIntegration.OVERDRIVE
        )
        script = ConfigureCollectionScript()
        output = StringIO()

        # We're going to change one value and add a new one.
        script.do_run(
            db.session,
            [
                "--name=Collection 1",
                "--url=foo",
                "--protocol=%s" % ExternalIntegration.BIBLIOTHECA,
            ],
            output,
        )

        # The collection has been changed.
        assert "foo" == collection.external_integration.url
        assert ExternalIntegration.BIBLIOTHECA == collection.protocol

        expect = (
            "Configuration settings stored.\n" + "\n".join(collection.explain()) + "\n"
        )

        assert expect == output.getvalue()


class TestShowIntegrationsScript:
    def test_with_no_integrations(self, db: DatabaseTransactionFixture):
        output = StringIO()
        ShowIntegrationsScript().do_run(db.session, output=output)
        assert "No integrations found.\n" == output.getvalue()

    def test_with_multiple_integrations(self, db: DatabaseTransactionFixture):
        i1 = db.external_integration(
            name="Integration 1", goal="Goal", protocol=ExternalIntegration.OVERDRIVE
        )
        i1.password = "a"
        i2 = db.external_integration(
            name="Integration 2", goal="Goal", protocol=ExternalIntegration.BIBLIOTHECA
        )
        i2.password = "b"

        # The output of this script is the result of running explain()
        # on both integrations.
        output = StringIO()
        ShowIntegrationsScript().do_run(db.session, output=output)
        expect_1 = "\n".join(i1.explain(include_secrets=False))
        expect_2 = "\n".join(i2.explain(include_secrets=False))

        assert expect_1 + "\n" + expect_2 + "\n" == output.getvalue()

        # We can tell the script to only list a single integration.
        output = StringIO()
        ShowIntegrationsScript().do_run(
            db.session, cmd_args=["--name=Integration 2"], output=output
        )
        assert expect_2 + "\n" == output.getvalue()

        # We can tell the script to include the integration secrets
        output = StringIO()
        ShowIntegrationsScript().do_run(
            db.session, cmd_args=["--show-secrets"], output=output
        )
        expect_1 = "\n".join(i1.explain(include_secrets=True))
        expect_2 = "\n".join(i2.explain(include_secrets=True))
        assert expect_1 + "\n" + expect_2 + "\n" == output.getvalue()


class TestConfigureIntegrationScript:
    def test_load_integration(self, db: DatabaseTransactionFixture):
        m = ConfigureIntegrationScript._integration

        with pytest.raises(ValueError) as excinfo:
            m(db.session, None, None, "protocol", None)
        assert (
            "An integration must by identified by either ID, name, or the combination of protocol and goal."
            in str(excinfo.value)
        )

        with pytest.raises(ValueError) as excinfo:
            m(db.session, "notanid", None, None, None)
        assert "No integration with ID notanid." in str(excinfo.value)

        with pytest.raises(ValueError) as excinfo:
            m(db.session, None, "Unknown integration", None, None)
        assert (
            'No integration with name "Unknown integration". To create it, you must also provide protocol and goal.'
            in str(excinfo.value)
        )

        integration = db.external_integration(protocol="Protocol", goal="Goal")
        integration.name = "An integration"
        assert integration == m(db.session, integration.id, None, None, None)

        assert integration == m(db.session, None, integration.name, None, None)

        assert integration == m(db.session, None, None, "Protocol", "Goal")

        # An integration may be created given a protocol and goal.
        integration2 = m(db.session, None, "I exist now", "Protocol", "Goal2")
        assert integration2 != integration
        assert "Protocol" == integration2.protocol
        assert "Goal2" == integration2.goal
        assert "I exist now" == integration2.name

    def test_add_settings(self, db: DatabaseTransactionFixture):
        script = ConfigureIntegrationScript()
        output = StringIO()

        script.do_run(
            db.session,
            [
                "--protocol=aprotocol",
                "--goal=agoal",
                "--setting=akey=avalue",
            ],
            output,
        )

        # An ExternalIntegration was created and configured.
        integration = get_one(
            db.session, ExternalIntegration, protocol="aprotocol", goal="agoal"
        )

        expect_output = (
            "Configuration settings stored.\n" + "\n".join(integration.explain()) + "\n"
        )
        assert expect_output == output.getvalue()


class TestShowLanesScript:
    def test_with_no_lanes(self, db: DatabaseTransactionFixture):
        output = StringIO()
        ShowLanesScript().do_run(db.session, output=output)
        assert "No lanes found.\n" == output.getvalue()

    def test_with_multiple_lanes(self, db: DatabaseTransactionFixture):
        l1 = db.lane()
        l2 = db.lane()

        # The output of this script is the result of running explain()
        # on both lanes.
        output = StringIO()
        ShowLanesScript().do_run(db.session, output=output)
        expect_1 = "\n".join(l1.explain())
        expect_2 = "\n".join(l2.explain())

        assert expect_1 + "\n\n" + expect_2 + "\n\n" == output.getvalue()

        # We can tell the script to only list a single lane.
        output = StringIO()
        ShowLanesScript().do_run(
            db.session, cmd_args=["--id=%s" % l2.id], output=output
        )
        assert expect_2 + "\n\n" == output.getvalue()


class TestConfigureLaneScript:
    def test_bad_arguments(self, db: DatabaseTransactionFixture):
        script = ConfigureLaneScript()

        # No lane id but no library short name for creating it either.
        with pytest.raises(ValueError) as excinfo:
            script.do_run(db.session, [])
        assert "Library short name is required to create a new lane" in str(
            excinfo.value
        )

        # Try to create a lane for a nonexistent library.
        with pytest.raises(ValueError) as excinfo:
            script.do_run(db.session, ["--library-short-name=nosuchlibrary"])
        assert 'No such library: "nosuchlibrary".' in str(excinfo.value)

    def test_create_lane(self, db: DatabaseTransactionFixture):
        script = ConfigureLaneScript()
        parent = db.lane()

        # Create a lane and set its attributes.
        output = StringIO()
        script.do_run(
            db.session,
            [
                "--library-short-name=%s" % db.default_library().short_name,
                "--parent-id=%s" % parent.id,
                "--priority=3",
                "--display-name=NewLane",
            ],
            output,
        )

        # The lane was created and configured properly.
        lane = get_one(db.session, Lane, display_name="NewLane")
        assert db.default_library() == lane.library
        assert parent == lane.parent
        assert 3 == lane.priority

        # The output explains the lane settings.
        expect = "Lane settings stored.\n" + "\n".join(lane.explain()) + "\n"
        assert expect == output.getvalue()

    def test_reconfigure_lane(self, db: DatabaseTransactionFixture):
        # The lane exists.
        lane = db.lane(display_name="Name")
        lane.priority = 3

        parent = db.lane()

        script = ConfigureLaneScript()
        output = StringIO()

        script.do_run(
            db.session,
            [
                "--id=%s" % lane.id,
                "--priority=1",
                "--parent-id=%s" % parent.id,
            ],
            output,
        )

        # The lane has been changed.
        assert 1 == lane.priority
        assert parent == lane.parent
        expect = "Lane settings stored.\n" + "\n".join(lane.explain()) + "\n"

        assert expect == output.getvalue()


class TestCollectionInputScript:
    """Test the ability to name collections on the command line."""

    def test_parse_command_line(self, db: DatabaseTransactionFixture):
        def collections(cmd_args):
            parsed = CollectionInputScript.parse_command_line(db.session, cmd_args)
            return parsed.collections

        # No collections named on command line -> no collections
        assert [] == collections([])

        # Nonexistent collection -> ValueError
        with pytest.raises(ValueError) as excinfo:
            collections(['--collection="no such collection"'])
        assert 'Unknown collection: "no such collection"' in str(excinfo.value)

        # Collections are presented in the order they were encountered
        # on the command line.
        c2 = db.collection()
        expect = [c2, db.default_collection()]
        args = ["--collection=" + c.name for c in expect]
        actual = collections(args)
        assert expect == actual


class TestCollectionArgumentsScript:
    """Test the ability to take collection arguments on the command line."""

    def test_parse_command_line(self, db: DatabaseTransactionFixture):
        def collections(cmd_args):
            parsed = CollectionArgumentsScript.parse_command_line(db.session, cmd_args)
            return parsed.collections

        # No collections named on command line -> no collections
        assert [] == collections([])

        # Nonexistent collection -> ValueError
        with pytest.raises(ValueError) as excinfo:
            collections(["no such collection"])
        assert "Unknown collection: no such collection" in str(excinfo.value)

        # Collections are presented in the order they were encountered
        # on the command line.
        c2 = db.collection()
        expect = [c2, db.default_collection()]
        args = [c.name for c in expect]
        actual = collections(args)
        assert expect == actual

        # It is okay to not specify any collections.
        expect = []
        args = [c.name for c in expect]
        actual = collections(args)
        assert expect == actual


# Mock classes used by TestOPDSImportScript
class MockOPDSImportMonitor:
    """Pretend to monitor an OPDS feed for new titles."""

    INSTANCES: list[MockOPDSImportMonitor] = []

    def __init__(self, _db, collection, *args, **kwargs):
        self.collection = collection
        self.args = args
        self.kwargs = kwargs
        self.INSTANCES.append(self)
        self.was_run = False

    def run(self):
        self.was_run = True


class MockOPDSImporter:
    """Pretend to import titles from an OPDS feed."""


class MockOPDSImportScript(OPDSImportScript):
    """Actually instantiate a monitor that will pretend to do something."""

    MONITOR_CLASS: type[OPDSImportMonitor] = MockOPDSImportMonitor  # type: ignore
    IMPORTER_CLASS = MockOPDSImporter  # type: ignore


class TestOPDSImportScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        db.default_collection().external_integration.setting(
            Collection.DATA_SOURCE_NAME_SETTING
        ).value = DataSource.OA_CONTENT_SERVER

        script = MockOPDSImportScript(db.session)
        script.do_run([])

        # Since we provided no collection, a MockOPDSImportMonitor
        # was instantiated for each OPDS Import collection in the database.
        monitor = MockOPDSImportMonitor.INSTANCES.pop()
        assert db.default_collection() == monitor.collection

        args = ["--collection=%s" % db.default_collection().name]
        script.do_run(args)

        # If we provide the collection name, a MockOPDSImportMonitor is
        # also instantiated.
        monitor = MockOPDSImportMonitor.INSTANCES.pop()
        assert db.default_collection() == monitor.collection
        assert True == monitor.was_run

        # Our replacement OPDS importer class was passed in to the
        # monitor constructor. If this had been a real monitor, that's the
        # code we would have used to import OPDS feeds.
        assert MockOPDSImporter == monitor.kwargs["import_class"]
        assert False == monitor.kwargs["force_reimport"]

        # Setting --force changes the 'force_reimport' argument
        # passed to the monitor constructor.
        args.append("--force")
        script.do_run(args)
        monitor = MockOPDSImportMonitor.INSTANCES.pop()
        assert db.default_collection() == monitor.collection
        assert True == monitor.kwargs["force_reimport"]


class MockWhereAreMyBooks(WhereAreMyBooksScript):
    """A mock script that keeps track of its output in an easy-to-test
    form, so we don't have to mess around with StringIO.
    """

    def __init__(self, _db=None, output=None, search=None):
        # In most cases a list will do fine for `output`.
        output = output or []

        # In most tests an empty mock will do for `search`.
        search = search or MockExternalSearchIndex()

        super().__init__(_db, output, search)
        self.output = []

    def out(self, s, *args):
        if args:
            self.output.append((s, list(args)))
        else:
            self.output.append(s)


class TestWhereAreMyBooksScript:
    def test_no_search_integration(self, db: DatabaseTransactionFixture):
        # We can't even get started without a working search integration.

        # We'll also test the out() method by mocking the script's
        # standard output and using the normal out() implementation.
        # In other tests, which have more complicated output, we mock
        # out(), so this verifies that output actually gets written
        # out.
        output = StringIO()
        pytest.raises(
            CannotLoadConfiguration, WhereAreMyBooksScript, db.session, output=output
        )
        assert (
            "Here's your problem: the search integration is missing or misconfigured.\n"
            == output.getvalue()
        )

    def test_overall_structure(self, db: DatabaseTransactionFixture):
        # Verify that run() calls the methods we expect.

        class Mock(MockWhereAreMyBooks):
            """Used to verify that the correct methods are called."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.delete_cached_feeds_called = False
                self.checked_libraries = []
                self.explained_collections = []

            def check_library(self, library):
                self.checked_libraries.append(library)

            def delete_cached_feeds(self):
                self.delete_cached_feeds_called = True

            def explain_collection(self, collection):
                self.explained_collections.append(collection)

        # If there are no libraries in the system, that's a big problem.
        script = Mock(db.session)
        script.run()
        assert [
            "There are no libraries in the system -- that's a problem.",
            "\n",
        ] == script.output

        # We still run the other checks, though.
        assert True == script.delete_cached_feeds_called

        # Make some libraries and some collections, and try again.
        library1 = db.default_library()
        library2 = db.library()

        collection1 = db.default_collection()
        collection2 = db.collection()

        script = Mock(db.session)
        script.run()

        # Every library in the collection was checked.
        assert {library1, library2} == set(script.checked_libraries)

        # delete_cached_feeds() was called.
        assert True == script.delete_cached_feeds_called

        # Every collection in the database was explained.
        assert {collection1, collection2} == set(script.explained_collections)

        # There only output were the newlines after the five method
        # calls. All other output happened inside the methods we
        # mocked.
        assert ["\n"] * 5 == script.output

        # Finally, verify the ability to use the command line to limit
        # the check to specific collections. (This isn't terribly useful
        # since checks now run very quickly.)
        script = Mock(db.session)
        script.run(cmd_args=["--collection=%s" % collection2.name])
        assert [collection2] == script.explained_collections

    def test_check_library(self, db: DatabaseTransactionFixture):
        # Give the default library a collection and a lane.
        library = db.default_library()
        collection = db.default_collection()
        lane = db.lane(library=library)

        script = MockWhereAreMyBooks(db.session)
        script.check_library(library)

        checking, has_collection, has_lanes = script.output
        assert ("Checking library %s", [library.name]) == checking
        assert (" Associated with collection %s.", [collection.name]) == has_collection
        assert (" Associated with %s lanes.", [1]) == has_lanes

        # This library has no collections and no lanes.
        library2 = db.library()
        script.output = []
        script.check_library(library2)
        checking, no_collection, no_lanes = script.output
        assert ("Checking library %s", [library2.name]) == checking
        assert " This library has no collections -- that's a problem." == no_collection
        assert " This library has no lanes -- that's a problem." == no_lanes

    def test_delete_cached_feeds(self, db: DatabaseTransactionFixture):
        groups = CachedFeed(type=CachedFeed.GROUPS_TYPE, pagination="")
        db.session.add(groups)
        not_groups = CachedFeed(type=CachedFeed.PAGE_TYPE, pagination="")
        db.session.add(not_groups)

        assert 2 == db.session.query(CachedFeed).count()

        script = MockWhereAreMyBooks(db.session)
        script.delete_cached_feeds()
        how_many, theyre_gone = script.output
        assert (
            "%d feeds in cachedfeeds table, not counting grouped feeds.",
            [1],
        ) == how_many
        assert " Deleting them all." == theyre_gone

        # Call it again, and we don't see "Deleting them all". There aren't
        # any to delete.
        script.output = []
        script.delete_cached_feeds()
        [how_many] = script.output
        assert (
            "%d feeds in cachedfeeds table, not counting grouped feeds.",
            [0],
        ) == how_many

    @staticmethod
    def check_explanation(
        db: DatabaseTransactionFixture,
        presentation_ready=1,
        not_presentation_ready=0,
        no_delivery_mechanisms=0,
        suppressed=0,
        not_owned=0,
        in_search_index=0,
        **kwargs,
    ):
        """Runs explain_collection() and verifies expected output."""
        script = MockWhereAreMyBooks(db.session, **kwargs)
        script.explain_collection(db.default_collection())
        out = script.output

        # This always happens.
        assert (
            'Examining collection "%s"',
            [db.default_collection().name],
        ) == out.pop(0)
        assert (" %d presentation-ready works.", [presentation_ready]) == out.pop(0)
        assert (
            " %d works not presentation-ready.",
            [not_presentation_ready],
        ) == out.pop(0)

        # These totals are only given if the numbers are nonzero.
        #
        if no_delivery_mechanisms:
            assert (
                " %d works are missing delivery mechanisms and won't show up.",
                [no_delivery_mechanisms],
            ) == out.pop(0)

        if suppressed:
            assert (
                " %d works have suppressed LicensePools and won't show up.",
                [suppressed],
            ) == out.pop(0)

        if not_owned:
            assert (
                " %d non-open-access works have no owned licenses and won't show up.",
                [not_owned],
            ) == out.pop(0)

        # Search engine statistics are always shown.
        assert (
            " %d works in the search index, expected around %d.",
            [in_search_index, presentation_ready],
        ) == out.pop(0)

    def test_no_presentation_ready_works(self, db: DatabaseTransactionFixture):
        # This work is not presentation-ready.
        work = db.work(with_license_pool=True)
        work.presentation_ready = False
        script = MockWhereAreMyBooks(db.session)
        self.check_explanation(
            presentation_ready=0,
            not_presentation_ready=1,
            db=db,
        )

    def test_no_delivery_mechanisms(self, db: DatabaseTransactionFixture):
        # This work has a license pool, but no delivery mechanisms.
        work = db.work(with_license_pool=True)
        for lpdm in work.license_pools[0].delivery_mechanisms:
            db.session.delete(lpdm)
        self.check_explanation(no_delivery_mechanisms=1, db=db)

    def test_suppressed_pool(self, db: DatabaseTransactionFixture):
        # This work has a license pool, but it's suppressed.
        work = db.work(with_license_pool=True)
        work.license_pools[0].suppressed = True
        self.check_explanation(suppressed=1, db=db)

    def test_no_licenses(self, db: DatabaseTransactionFixture):
        # This work has a license pool, but no licenses owned.
        work = db.work(with_license_pool=True)
        work.license_pools[0].licenses_owned = 0
        self.check_explanation(not_owned=1, db=db)

    def test_search_engine(self, db: DatabaseTransactionFixture):
        output = StringIO()
        search = MockExternalSearchIndex()
        work = db.work(with_license_pool=True)
        work.presentation_ready = True
        search.bulk_update([work])

        # This MockExternalSearchIndex will always claim there is one
        # result.
        self.check_explanation(search=search, in_search_index=1, db=db)


class TestExplain:
    def test_explain(self, db: DatabaseTransactionFixture):
        """Make sure the Explain script runs without crashing."""
        work = db.work(with_license_pool=True, genre="Science Fiction")
        [pool] = work.license_pools
        edition = work.presentation_edition
        identifier = pool.identifier
        source = DataSource.lookup(db.session, DataSource.OCLC_LINKED_DATA)
        CoverageRecord.add_for(identifier, source, "an operation")
        input = StringIO()
        output = StringIO()
        args = ["--identifier-type", "Database ID", str(identifier.id)]
        Explain(db.session).do_run(cmd_args=args, stdin=input, stdout=output)
        output = output.getvalue()

        # The script ran. Spot-check that it provided various
        # information about the work, without testing the exact
        # output.
        assert pool.collection.name in output
        assert "Available to libraries: default" in output
        assert work.title in output
        assert "Science Fiction" in output
        for contributor in edition.contributors:
            assert contributor.sort_name in output

        # CoverageRecords associated with the primary identifier were
        # printed out.
        assert "OCLC Linked Data | an operation | success" in output

        # WorkCoverageRecords associated with the work were
        # printed out.
        assert "generate-opds | success" in output

        # There is an active LicensePool that is fulfillable and has
        # copies owned.
        assert "%s owned" % pool.licenses_owned in output
        assert "Fulfillable" in output
        assert "ACTIVE" in output


class TestReclassifyWorksForUncheckedSubjectsScript:
    def test_constructor(self, db: DatabaseTransactionFixture):
        """Make sure that we're only going to classify works
        with unchecked subjects.
        """
        script = ReclassifyWorksForUncheckedSubjectsScript(db.session)
        assert (
            WorkClassificationScript.policy
            == ReclassifyWorksForUncheckedSubjectsScript.policy
        )
        assert 100 == script.batch_size

        # Assert all joins have been included in the Order By
        ordered_by = script.query._order_by
        for join in script.query._join_entities:
            assert join.columns.id in ordered_by
        assert Work.id in ordered_by

    def test_paginate(self, db: DatabaseTransactionFixture):
        """Pagination is changed to be row-wise comparison
        Ensure we are paginating correctly within the same Subject page"""
        subject = db.subject(Subject.AXIS_360_AUDIENCE, "Any")
        works = []
        for i in range(20):
            work: Work = db.work(with_license_pool=True)
            db.classification(
                work.presentation_edition.primary_identifier,
                subject,
                work.license_pools[0].data_source,
            )
            works.append(work)

        script = ReclassifyWorksForUncheckedSubjectsScript(db.session)
        script.batch_size = 1
        for ix, [work] in enumerate(script.paginate_query(script.query)):
            # We are coming in via "id" order
            assert work == works[ix]
        assert ix == 19

        other_subject = db.subject(Subject.BISAC, "Any")
        last_work = works[-1]
        db.classification(
            last_work.presentation_edition.primary_identifier,
            other_subject,
            last_work.license_pools[0].data_source,
        )
        script.batch_size = 100
        next_works = next(script.paginate_query(script.query))
        # Works are only iterated over ONCE per loop
        assert len(next_works) == 20

        # A checked subjects work is not included
        not_work = db.work(with_license_pool=True)
        another_subject = db.subject(Subject.DDC, "Any")
        db.classification(
            not_work.presentation_edition.primary_identifier,
            another_subject,
            not_work.license_pools[0].data_source,
        )
        another_subject.checked = True
        db.session.commit()
        next_works = next(script.paginate_query(script.query))
        assert len(next_works) == 20
        assert not_work not in next_works

    def test_subject_checked(self, db: DatabaseTransactionFixture):
        subject = db.subject(Subject.AXIS_360_AUDIENCE, "Any")
        assert subject.checked == False

        works = []
        for i in range(10):
            work: Work = db.work(with_license_pool=True)
            db.classification(
                work.presentation_edition.primary_identifier,
                subject,
                work.license_pools[0].data_source,
            )
            works.append(work)

        script = ReclassifyWorksForUncheckedSubjectsScript(db.session)
        script.run()
        db.session.refresh(subject)
        assert subject.checked == True


class TestListCollectionMetadataIdentifiersScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        output = StringIO()
        script = ListCollectionMetadataIdentifiersScript(_db=db.session, output=output)

        # Create two collections.
        c1 = db.collection(external_account_id=db.fresh_url())
        c2 = db.collection(
            name="Local Over",
            protocol=ExternalIntegration.OVERDRIVE,
            external_account_id="banana",
        )

        script.do_run()

        def expected(c):
            return "({}) {}/{} => {}\n".format(
                str(c.id),
                c.name,
                c.protocol,
                c.metadata_identifier,
            )

        # In the output, there's a header, a line describing the format,
        # metdata identifiers for each collection, and a count of the
        # collections found.
        output = output.getvalue()
        assert "COLLECTIONS" in output
        assert "(id) name/protocol => metadata_identifier\n" in output
        assert expected(c1) in output
        assert expected(c2) in output
        assert "2 collections found.\n" in output


class TestMirrorResourcesScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        has_uploader = db.collection()
        mock_uploader = object()

        class Mock(MirrorResourcesScript):

            processed = []

            def collections_with_uploader(self, collections, collection_type):
                # Pretend that `has_uploader` is the only Collection
                # with an uploader.
                for collection in collections:
                    if collection == has_uploader:
                        yield collection, mock_uploader

            def process_collection(self, collection, policy):
                self.processed.append((collection, policy))

        script = Mock(db.session)

        # If there are no command-line arguments, process_collection
        # is called on every Collection in the system that is okayed
        # by collections_with_uploader.
        script.do_run(cmd_args=[])
        processed = script.processed.pop()
        assert (has_uploader, mock_uploader) == processed
        assert [] == script.processed

        # If a Collection is named on the command line,
        # process_collection is called on that Collection _if_ it has
        # an uploader.
        args = ["--collection=%s" % db.default_collection().name]
        script.do_run(cmd_args=args)
        assert [] == script.processed

        script.do_run(cmd_args=["--collection=%s" % has_uploader.name])
        processed = script.processed.pop()
        assert (has_uploader, mock_uploader) == processed

    @pytest.mark.parametrize(
        "name,collection_type,book_mirror_type,protocol,uploader_class,settings",
        [
            (
                "containing_open_access_books_with_s3_uploader",
                CollectionType.OPEN_ACCESS,
                ExternalIntegrationLink.OPEN_ACCESS_BOOKS,
                ExternalIntegration.S3,
                S3Uploader,
                None,
            ),
            (
                "containing_protected_access_books_with_s3_uploader",
                CollectionType.PROTECTED_ACCESS,
                ExternalIntegrationLink.PROTECTED_ACCESS_BOOKS,
                ExternalIntegration.S3,
                S3Uploader,
                None,
            ),
            (
                "containing_open_access_books_with_minio_uploader",
                CollectionType.OPEN_ACCESS,
                ExternalIntegrationLink.OPEN_ACCESS_BOOKS,
                ExternalIntegration.MINIO,
                MinIOUploader,
                {MinIOUploaderConfiguration.ENDPOINT_URL: "http://localhost"},
            ),
            (
                "containing_protected_access_books_with_minio_uploader",
                CollectionType.PROTECTED_ACCESS,
                ExternalIntegrationLink.PROTECTED_ACCESS_BOOKS,
                ExternalIntegration.MINIO,
                MinIOUploader,
                {MinIOUploaderConfiguration.ENDPOINT_URL: "http://localhost"},
            ),
        ],
    )
    def test_collections(
        self,
        db,
        name,
        collection_type,
        book_mirror_type,
        protocol,
        uploader_class,
        settings,
    ):
        class Mock(MirrorResourcesScript):

            mock_policy = object()

            @classmethod
            def replacement_policy(cls, uploader):
                cls.replacement_policy_called_with = uploader
                return cls.mock_policy

        script = Mock()

        # The default collection does not have an uploader.
        # This new collection does.
        has_uploader = db.collection()
        mirror = db.external_integration(protocol, ExternalIntegration.STORAGE_GOAL)

        if settings:
            for key, value in settings.items():
                mirror.setting(key).value = value

        integration_link = db.external_integration_link(
            integration=has_uploader._external_integration,
            other_integration=mirror,
            purpose=ExternalIntegrationLink.COVERS,
        )

        # Calling collections_with_uploader will do nothing for collections
        # that don't have an uploader. It will make a MirrorUploader for
        # the other collection, pass it into replacement_policy,
        # and yield the result.
        result = script.collections_with_uploader(
            [
                db.default_collection(),
                has_uploader,
                db.default_collection(),
            ],
            collection_type,
        )

        [(collection, policy)] = result
        assert has_uploader == collection
        assert Mock.mock_policy == policy
        # The mirror uploader was associated with a purpose of "covers", so we only
        # expect to have one MirrorUploader.
        assert Mock.replacement_policy_called_with[book_mirror_type] == None
        assert isinstance(
            Mock.replacement_policy_called_with[ExternalIntegrationLink.COVERS],
            MirrorUploader,
        )

        # Add another storage for books.
        another_mirror = db.external_integration(
            protocol, ExternalIntegration.STORAGE_GOAL
        )

        integration_link = db.external_integration_link(
            integration=has_uploader._external_integration,
            other_integration=another_mirror,
            purpose=book_mirror_type,
        )

        result = script.collections_with_uploader(
            [
                db.default_collection(),
                has_uploader,
                db.default_collection(),
            ],
            collection_type,
        )

        [(collection, policy)] = result
        assert has_uploader == collection
        assert Mock.mock_policy == policy
        # There should be two MirrorUploaders, one for each purpose.
        assert isinstance(
            Mock.replacement_policy_called_with[ExternalIntegrationLink.COVERS],
            uploader_class,
        )
        assert isinstance(
            Mock.replacement_policy_called_with[book_mirror_type], uploader_class
        )

    def test_replacement_policy(self):
        uploader = object()
        p = MirrorResourcesScript.replacement_policy(uploader)
        assert uploader == p.mirrors
        assert True == p.link_content
        assert True == p.even_if_not_apparently_updated
        assert False == p.rights

    def test_process_collection(self, db: DatabaseTransactionFixture):
        class MockScript(MirrorResourcesScript):
            process_item_called_with = []

            def process_item(self, collection, link, policy):
                self.process_item_called_with.append((collection, link, policy))

        # Mock the Hyperlink.unmirrored method
        link1 = object()
        link2 = object()

        def unmirrored(collection):
            assert collection == db.default_collection()
            yield link1
            yield link2

        script = MockScript(db.session)
        policy = object()
        script.process_collection(db.default_collection(), policy, unmirrored)

        # Process_collection called unmirrored() and then called process_item
        # on every item yielded by unmirrored()
        call1, call2 = script.process_item_called_with
        assert (db.default_collection(), link1, policy) == call1
        assert (db.default_collection(), link2, policy) == call2

    def test_derive_rights_status(self, db: DatabaseTransactionFixture):
        """Test our ability to determine the rights status of a Resource,
        in the absence of immediate information from the server.
        """
        m = MirrorResourcesScript.derive_rights_status
        work = db.work(with_open_access_download=True)
        [pool] = work.license_pools
        [lpdm] = pool.delivery_mechanisms
        resource = lpdm.resource

        expect = lpdm.rights_status.uri

        # Given the LicensePool, we can figure out the Resource's
        # rights status based on what was previously recovered. This lets
        # us know whether it's okay to mirror that Resource.
        assert expect == m(pool, resource)

        # In theory, a Resource can be associated with several
        # LicensePoolDeliveryMechanisms. That's why a LicensePool is
        # necessary -- to see which LicensePoolDeliveryMechanism we're
        # looking at.
        assert None == m(None, resource)

        # If there's no Resource-specific information, but a
        # LicensePool has only one rights URI among all of its
        # LicensePoolDeliveryMechanisms, then we can assume all Resources
        # for that LicensePool use that same set of rights.
        w2 = db.work(with_license_pool=True)
        [pool2] = w2.license_pools
        assert pool2.delivery_mechanisms[0].rights_status.uri == m(pool2, None)

        # If there's more than one possibility, or the LicensePool has
        # no LicensePoolDeliveryMechanisms at all, then we just don't
        # know.
        pool2.set_delivery_mechanism(
            content_type="text/plain", drm_scheme=None, rights_uri=RightsStatus.CC_BY_ND
        )
        assert None == m(pool2, None)

        pool2.delivery_mechanisms = []
        assert None == m(pool2, None)

    def test_process_item(self, db: DatabaseTransactionFixture):
        """Test the code that actually sets up the mirror operation."""
        # Every time process_item() is called, it's either going to ask
        # this thing to mirror the item, or it's going to decide not to.
        class MockMirrorUtility:
            def __init__(self):
                self.mirrored = []

            def mirror_link(self, **kwargs):
                self.mirrored.append(kwargs)

        mirror = MockMirrorUtility()

        class MockScript(MirrorResourcesScript):
            MIRROR_UTILITY = mirror
            RIGHTS_STATUS = None

            def derive_rights_status(self, license_pool, resource):
                """Always return the same rights status information.
                To start out, act like no rights information is available.
                """
                self.derive_rights_status_called_with = (license_pool, resource)
                return self.RIGHTS_STATUS

        # Resource and Hyperlink are a pain to use for real, so here
        # are some cheap mocks.
        class MockResource:
            def __init__(self, url):
                self.url = url

        class MockLink:
            def __init__(self, rel, href, identifier):
                self.rel = rel
                self.resource = MockResource(href)
                self.identifier = identifier

        script = MockScript(db.session)
        m = script.process_item

        # If we can't tie the Hyperlink to a LicensePool in the given
        # Collection, no upload happens. (This shouldn't happen
        # because Hyperlink.unmirrored only finds Hyperlinks
        # associated with Identifiers licensed through a Collection.)
        identifier = db.identifier()
        policy = object()
        download_link = MockLink(
            Hyperlink.OPEN_ACCESS_DOWNLOAD, db.fresh_url(), identifier
        )
        db.default_collection().data_source = DataSource.GUTENBERG
        m(db.default_collection(), download_link, policy)
        assert [] == mirror.mirrored

        # This HyperLink does match a LicensePool, but it's not
        # in the collection we're mirroring, so mirroring it might not be
        # appropriate.
        work = db.work(
            with_open_access_download=True, collection=db.default_collection()
        )
        pool = work.license_pools[0]
        download_link.identifier = pool.identifier
        wrong_collection = db.collection()
        wrong_collection.data_source = DataSource.GUTENBERG
        m(wrong_collection, download_link, policy)
        assert [] == mirror.mirrored

        # For "open-access" downloads of actual books, if we can't
        # determine the actual rights status of the book, then we
        # don't do anything.
        m(db.default_collection(), download_link, policy)
        assert [] == mirror.mirrored
        assert (pool, download_link.resource) == script.derive_rights_status_called_with

        # If we _can_ determine the rights status, a mirror attempt is made.
        script.RIGHTS_STATUS = object()
        m(db.default_collection(), download_link, policy)
        attempt = mirror.mirrored.pop()
        assert policy == attempt["policy"]
        assert pool.data_source == attempt["data_source"]
        assert pool == attempt["model_object"]
        assert download_link == attempt["link_obj"]

        link = attempt["link"]
        assert isinstance(link, LinkData)
        assert download_link.resource.url == link.href

        # For other types of links, we rely on fair use, so the "rights
        # status" doesn't matter.
        script.RIGHTS_STATUS = None
        thumb_link = MockLink(
            Hyperlink.THUMBNAIL_IMAGE, db.fresh_url(), pool.identifier
        )
        m(db.default_collection(), thumb_link, policy)
        attempt = mirror.mirrored.pop()
        assert thumb_link.resource.url == attempt["link"].href


class TestRebuildSearchIndexScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        class MockSearchIndex:
            def setup_index(self):
                # This is where the search index is deleted and recreated.
                self.setup_index_called = True

            def bulk_update(self, works):
                self.bulk_update_called_with = list(works)
                return works, []

        index = MockSearchIndex()
        work = db.work(with_license_pool=True)
        work2 = db.work(with_license_pool=True)
        wcr = WorkCoverageRecord
        decoys = [wcr.QUALITY_OPERATION, wcr.GENERATE_MARC_OPERATION]

        # Set up some coverage records.
        for operation in decoys + [wcr.UPDATE_SEARCH_INDEX_OPERATION]:
            for w in (work, work2):
                wcr.add_for(w, operation, status=random.choice(wcr.ALL_STATUSES))

        coverage_qu = db.session.query(wcr).filter(
            wcr.operation == wcr.UPDATE_SEARCH_INDEX_OPERATION
        )
        original_coverage = [x.id for x in coverage_qu]

        # Run the script.
        script = RebuildSearchIndexScript(db.session, search_index_client=index)
        [progress] = script.do_run()

        # The mock methods were called with the values we expect.
        assert True == index.setup_index_called
        assert {work, work2} == set(index.bulk_update_called_with)

        # The script returned a list containing a single
        # CoverageProviderProgress object containing accurate
        # information about what happened (from the CoverageProvider's
        # point of view).
        assert (
            "Items processed: 2. Successes: 2, transient failures: 0, persistent failures: 0"
            == progress.achievements
        )

        # The old WorkCoverageRecords for the works were deleted. Then
        # the CoverageProvider did its job and new ones were added.
        new_coverage = [x.id for x in coverage_qu]
        assert 2 == len(new_coverage)
        assert set(new_coverage) != set(original_coverage)


class TestSearchIndexCoverageRemover:

    SERVICE_NAME = "Search Index Coverage Remover"

    def test_do_run(self, db: DatabaseTransactionFixture):
        work = db.work()
        work2 = db.work()
        wcr = WorkCoverageRecord
        decoys = [wcr.QUALITY_OPERATION, wcr.GENERATE_MARC_OPERATION]

        # Set up some coverage records.
        for operation in decoys + [wcr.UPDATE_SEARCH_INDEX_OPERATION]:
            for w in (work, work2):
                wcr.add_for(w, operation, status=random.choice(wcr.ALL_STATUSES))

        # Run the script.
        script = SearchIndexCoverageRemover(db.session)
        result = script.do_run()
        assert isinstance(result, TimestampData)
        assert "Coverage records deleted: 2" == result.achievements

        # UPDATE_SEARCH_INDEX_OPERATION records have been removed.
        # No other records are affected.
        for w in (work, work2):
            remaining = [x.operation for x in w.coverage_records]
            assert sorted(remaining) == sorted(decoys)


class TestUpdateLaneSizeScript:
    def test_do_run(
        self,
        db,
        external_search_patch_fixture: ExternalSearchPatchFixture,
    ):
        lane = db.lane()
        lane.size = 100
        UpdateLaneSizeScript(db.session).do_run(cmd_args=[])
        assert 0 == lane.size

    def test_should_process_lane(self, db: DatabaseTransactionFixture):
        """Only Lane objects can have their size updated."""
        lane = db.lane()
        script = UpdateLaneSizeScript(db.session)
        assert True == script.should_process_lane(lane)

        worklist = WorkList()
        assert False == script.should_process_lane(worklist)

    def test_site_configuration_has_changed(
        self,
        db: DatabaseTransactionFixture,
        external_search_patch_fixture: ExternalSearchPatchFixture,
    ):
        lane1 = db.lane()
        lane2 = db.lane()
        lane1.size = 100
        lane2.size = 50

        # Commit changes to the DB so the lane creation listeners are fired
        db.session.commit()

        with patch("core.lane.site_configuration_has_changed") as lane_changed:
            with patch(
                "core.scripts.site_configuration_has_changed"
            ) as scripts_changed:
                UpdateLaneSizeScript(db.session).do_run(cmd_args=[])

        assert 0 == lane1.size
        assert 0 == lane2.size

        # The listeners in lane.py shouldn't call site_configuration_has_changed
        lane_changed.assert_not_called()

        # The script should call site_configuration_has_changed once when it is done
        scripts_changed.assert_called_once()


class TestUpdateCustomListSizeScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        customlist, ignore = db.customlist(num_entries=1)
        customlist.library = db.default_library()
        customlist.size = 100
        UpdateCustomListSizeScript(db.session).do_run(cmd_args=[])
        assert 1 == customlist.size


class TestDeleteInvisibleLanesScript:
    def test_do_run(self, db: DatabaseTransactionFixture):
        """Test that invisible lanes and their visible children are deleted."""
        # create a library
        short_name = "TESTLIB"
        l1 = db.library("test library", short_name=short_name)
        # with a set of default lanes
        create_default_lanes(db.session, l1)

        # verify there is a top level visible Fiction lane
        top_level_fiction_lane: Lane = (
            db.session.query(Lane)
            .filter(Lane.library == l1)
            .filter(Lane.parent == None)
            .filter(Lane.display_name == "Fiction")
            .order_by(Lane.priority)
            .one()
        )

        first_child_id = top_level_fiction_lane.children[0].id

        assert top_level_fiction_lane is not None
        assert top_level_fiction_lane.visible == True
        assert first_child_id is not None

        # run script and verify that it had no effect:
        DeleteInvisibleLanesScript(_db=db.session).do_run([short_name])
        top_level_fiction_lane: Lane = (
            db.session.query(Lane)
            .filter(Lane.library == l1)
            .filter(Lane.parent == None)
            .filter(Lane.display_name == "Fiction")
            .order_by(Lane.priority)
            .one()
        )
        assert top_level_fiction_lane is not None

        # flag as deleted
        top_level_fiction_lane.visible = False

        # and now run script.
        DeleteInvisibleLanesScript(_db=db.session).do_run([short_name])

        # verify the lane has now been deleted.
        deleted_lane = (
            db.session.query(Lane)
            .filter(Lane.library == l1)
            .filter(Lane.parent == None)
            .filter(Lane.display_name == "Fiction")
            .order_by(Lane.priority)
            .all()
        )

        assert deleted_lane == []

        # verify the first child was also deleted:

        first_child_lane = (
            db.session.query(Lane).filter(Lane.id == first_child_id).all()
        )

        assert first_child_lane == []


class TestCustomListUpdateEntriesScriptData:
    populated_books: list[Work]
    unpopular_books: list[Work]


class TestCustomListUpdateEntriesScript:
    @staticmethod
    def _populate_works(
        data: EndToEndSearchFixture,
    ) -> TestCustomListUpdateEntriesScriptData:
        db = data.external_search.db

        result = TestCustomListUpdateEntriesScriptData()
        result.populated_books = [
            db.work(with_license_pool=True, title="Populated Book") for _ in range(5)
        ]
        result.unpopular_books = [
            db.work(with_license_pool=True, title="Unpopular Book") for _ in range(3)
        ]
        # This is for back population only
        result.populated_books[0].license_pools[
            0
        ].availability_time = datetime.datetime(1900, 1, 1)
        db.session.commit()
        return result

    def test_process_custom_list(
        self,
        end_to_end_search_fixture: EndToEndSearchFixture,
    ):
        fixture = end_to_end_search_fixture
        db, session = (
            fixture.external_search.db,
            fixture.external_search.db.session,
        )
        data = self._populate_works(fixture)
        fixture.populate_search_index()

        last_updated = datetime.datetime.now() - datetime.timedelta(hours=1)
        custom_list, _ = db.customlist()
        custom_list.library = db.default_library()
        custom_list.auto_update_enabled = True
        custom_list.auto_update_query = json.dumps(
            dict(query=dict(key="title", value="Populated Book"))
        )
        custom_list.auto_update_last_update = last_updated
        custom_list.auto_update_status = CustomList.UPDATED

        custom_list1, _ = db.customlist()
        custom_list1.library = db.default_library()
        custom_list1.auto_update_enabled = True
        custom_list1.auto_update_query = json.dumps(
            dict(query=dict(key="title", value="Unpopular Book"))
        )
        custom_list1.auto_update_last_update = last_updated
        custom_list1.auto_update_status = CustomList.UPDATED

        # Do the process
        script = CustomListUpdateEntriesScript(session)
        mock_parse = MagicMock()
        mock_parse.return_value.libraries = [db.default_library()]
        script.parse_command_line = mock_parse

        with freeze_time("2022-01-01") as frozen_time:
            script.run()

        session.refresh(custom_list)
        session.refresh(custom_list1)
        assert (
            len(custom_list.entries) == 1 + len(data.populated_books) - 1
        )  # default + new - one past availability time
        assert custom_list.size == 1 + len(data.populated_books) - 1
        assert len(custom_list1.entries) == 1 + len(
            data.unpopular_books
        )  # default + new
        assert custom_list1.size == 1 + len(data.unpopular_books)
        # last updated time has updated correctly
        assert custom_list.auto_update_last_update == frozen_time.time_to_freeze
        assert custom_list1.auto_update_last_update == frozen_time.time_to_freeze

    def test_search_facets(self, end_to_end_search_fixture: EndToEndSearchFixture):
        with patch("core.query.customlist.ExternalSearchIndex") as mock_index:
            fixture = end_to_end_search_fixture
            db, session = (
                fixture.external_search.db,
                fixture.external_search.db.session,
            )
            data = self._populate_works(fixture)
            fixture.populate_search_index()

            last_updated = datetime.datetime.now() - datetime.timedelta(hours=1)
            custom_list, _ = db.customlist()
            custom_list.library = db.default_library()
            custom_list.auto_update_enabled = True
            custom_list.auto_update_query = json.dumps(
                dict(query=dict(key="title", value="Populated Book"))
            )
            custom_list.auto_update_facets = json.dumps(
                dict(order="title", languages="fr", media=["book", "audio"])
            )
            custom_list.auto_update_last_update = last_updated

            script = CustomListUpdateEntriesScript(session)
            script.process_custom_list(custom_list)

            assert mock_index().query_works.call_count == 1
            filter: Filter = mock_index().query_works.call_args_list[0][0][1]
            assert filter.sort_order[0] == {
                "sort_title": "asc"
            }  # since we asked for title ordering this should come up first
            assert filter.languages == ["fr"]
            assert filter.media == ["book", "audio"]

    @freeze_time("2022-01-01", as_kwarg="frozen_time")
    def test_no_last_update(
        self,
        end_to_end_search_fixture: EndToEndSearchFixture,
        frozen_time=None,
    ):
        fixture = end_to_end_search_fixture
        db, session = (
            fixture.external_search.db,
            fixture.external_search.db.session,
        )
        data = self._populate_works(fixture)
        fixture.populate_search_index()

        # No previous timestamp
        custom_list, _ = db.customlist()
        custom_list.library = db.default_library()
        custom_list.auto_update_enabled = True
        custom_list.auto_update_query = json.dumps(
            dict(query=dict(key="title", value="Populated Book"))
        )
        script = CustomListUpdateEntriesScript(session)
        script.process_custom_list(custom_list)
        assert custom_list.auto_update_last_update == frozen_time.time_to_freeze

    def test_init_backpopulates(
        self,
        end_to_end_search_fixture: EndToEndSearchFixture,
    ):
        with patch("core.scripts.CustomListQueries") as mock_queries:
            fixture = end_to_end_search_fixture
            db, session = (
                fixture.external_search.db,
                fixture.external_search.db.session,
            )
            data = self._populate_works(fixture)
            fixture.populate_search_index()

            custom_list, _ = db.customlist()
            custom_list.library = db.default_library()
            custom_list.auto_update_enabled = True
            custom_list.auto_update_query = json.dumps(
                dict(query=dict(key="title", value="Populated Book"))
            )
            script = CustomListUpdateEntriesScript(session)
            script.process_custom_list(custom_list)

            args = mock_queries.populate_query_pages.call_args_list[0]
            assert args[1]["json_query"] == None
            assert args[1]["start_page"] == 2
            assert custom_list.auto_update_status == CustomList.UPDATED

    def test_repopulate_state(
        self,
        end_to_end_search_fixture: EndToEndSearchFixture,
    ):
        """The repopulate deletes all entries and runs the query again"""
        fixture = end_to_end_search_fixture
        db, session = (
            fixture.external_search.db,
            fixture.external_search.db.session,
        )
        data = self._populate_works(fixture)
        fixture.populate_search_index()

        custom_list, _ = db.customlist()
        custom_list.library = db.default_library()
        custom_list.auto_update_enabled = True
        custom_list.auto_update_query = json.dumps(
            dict(query=dict(key="title", value="Populated Book"))
        )
        custom_list.auto_update_status = CustomList.REPOPULATE

        # Previously the list would have had Unpopular books
        for w in data.unpopular_books:
            custom_list.add_entry(w)
        prev_entry = custom_list.entries[0]

        script = CustomListUpdateEntriesScript(session)
        script.process_custom_list(custom_list)
        # Commit the process changes and refresh the list
        session.commit()
        session.refresh(custom_list)

        # Now the entries are only the Popular books
        assert {e.work_id for e in custom_list.entries} == {
            w.id for w in data.populated_books
        }
        # The previous entries should have been deleted, not just un-related
        with pytest.raises(InvalidRequestError):
            session.refresh(prev_entry)
        assert custom_list.auto_update_status == CustomList.UPDATED
        assert custom_list.size == len(data.populated_books)


class TestWorkConsolidationScript:
    """TODO"""


class TestWorkPresentationScript:
    """TODO"""


class TestWorkClassificationScript:
    """TODO"""


class TestWorkOPDSScript:
    """TODO"""


class TestCustomListManagementScript:
    """TODO"""


class TestNYTBestSellerListsScript:
    """TODO"""
