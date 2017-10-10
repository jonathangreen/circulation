from nose.tools import (
    assert_raises,
    eq_,
    set_trace,
)
import logging
import time
from psycopg2.extras import NumericRange

from . import (
    DatabaseTest,
)

from lane import Lane
from model import (
    Edition,
    ExternalIntegration,
)
from external_search import (
    ExternalSearchIndex,
    ExternalSearchIndexVersions,
    DummyExternalSearchIndex,
    SearchIndexCoverageProvider,
)
from classifier import Classifier


class ExternalSearchTest(DatabaseTest):
    """
    These tests require elasticsearch to be running locally. If it's not, or there's
    an error creating the index, the tests will pass without doing anything.

    Tests for elasticsearch are useful for ensuring that we haven't accidentally broken
    a type of search by changing analyzers or queries, but search needs to be tested manually
    to ensure that it works well overall, with a realistic index.
    """

    def setup(self):
        super(ExternalSearchTest, self).setup(mock_search=False)

        self.integration = self._external_integration(
            ExternalIntegration.ELASTICSEARCH,
            goal=ExternalIntegration.SEARCH_GOAL,
            url=u'http://localhost:9200',
            settings={ExternalSearchIndex.WORKS_INDEX_KEY : u'test_index-v0'}
        )

        try:
            self.search = ExternalSearchIndex(self._db)
        except Exception as e:
            self.search = None
            print "Unable to set up elasticsearch index, search tests will be skipped."
            print e

    def teardown(self):
        if self.search:
            if self.search.works_index:
                self.search.indices.delete(self.search.works_index, ignore=[404])
            self.search.indices.delete('the_other_index', ignore=[404])
            self.search.indices.delete('test_index-v100', ignore=[404])
            ExternalSearchIndex.reset()
        super(ExternalSearchTest, self).teardown()


class TestExternalSearch(ExternalSearchTest):

    def test_works_index_name(self):
        if not self.search:
            return
        eq_("test_index-v0", self.search.works_index_name(self._db))

    def test_setup_index_creates_new_index(self):
        if not self.search:
            return

        current_index = self.search.works_index
        self.search.setup_index(new_index='the_other_index')

        # Both indices exist.
        eq_(True, self.search.indices.exists(current_index))
        eq_(True, self.search.indices.exists('the_other_index'))

        # The index for the app's search is still the original index.
        eq_(current_index, self.search.works_index)

        # The alias hasn't been passed over to the new index.
        alias = 'test_index' + self.search.CURRENT_ALIAS_SUFFIX
        eq_(alias, self.search.works_alias)
        eq_(True, self.search.indices.exists_alias(current_index, alias))
        eq_(False, self.search.indices.exists_alias('the_other_index', alias))

    def test_set_works_index_and_alias(self):
        if not self.search:
            return

        # If -current alias is given but doesn't exist, the appropriate
        # index and alias will be created.
        self.search.set_works_index_and_alias('banana-current')

        expected_index = 'banana-' + ExternalSearchIndexVersions.latest()
        eq_(expected_index, self.search.works_index)
        eq_('banana-current', self.search.works_alias)

    def test_setup_current_alias(self):
        if not self.search:
            return

        # The index was generated from the string in configuration.
        index_name = 'test_index-v0'
        eq_(index_name, self.search.works_index)
        eq_(True, self.search.indices.exists(index_name))

        # The alias is also created from the configuration.
        alias = 'test_index' + self.search.CURRENT_ALIAS_SUFFIX
        eq_(alias, self.search.works_alias)
        eq_(True, self.search.indices.exists_alias(index_name, alias))

        # If the -current alias is already set on a different index, it
        # won't be reassigned. Instead, search will occur against the
        # index itself.
        ExternalSearchIndex.reset()
        self.integration.set_setting(ExternalSearchIndex.WORKS_INDEX_KEY, u'test_index-v100')
        self.search = ExternalSearchIndex(self._db)

        eq_('test_index-v100', self.search.works_index)
        eq_('test_index-v100', self.search.works_alias)

    def test_transfer_current_alias(self):
        if not self.search:
            return

        # If the index doesn't exist, an error is raised.
        assert_raises(
            ValueError, self.search.transfer_current_alias, 'test_index-v3')

        original_index = self.search.works_index

        # If the -current alias doesn't exist, it's created
        # and everything is updated accordingly.
        self.search.indices.delete_alias(
            index=original_index, name='test_index-current'
        )
        self.search.setup_index(new_index='test_index-v100')
        self.search.transfer_current_alias('test_index-v100')
        eq_('test_index-v100', self.search.works_index)
        eq_('test_index-current', self.search.works_alias)

        # If the -current alias already exists on the index,
        # it's used without a problem.
        self.search.transfer_current_alias('test_index-v100')
        eq_('test_index-v100', self.search.works_index)
        eq_('test_index-current', self.search.works_alias)

        # If the -current alias is being used on a different version of the
        # index, it's deleted from that index and placed on the new one.
        self.search.setup_index(original_index)
        self.search.transfer_current_alias(original_index)
        eq_(original_index, self.search.works_index)
        eq_('test_index-current', self.search.works_alias)

        # It has been removed from other index.
        eq_(False, self.search.indices.exists_alias(
            index='test_index-v100', name='test_index-current'))
        # And only exists on the new index.
        alias_indices = self.search.indices.get_alias(name='test_index-current').keys()
        eq_(['test_index-v0'], alias_indices)

        # If the index doesn't have the same base name, an error is raised.
        assert_raises(
            ValueError, self.search.transfer_current_alias, 'banana-v10')

class TestExternalSearchWithWorks(ExternalSearchTest):
    """These tests run against a real search index with works in it.
    The setup is very slow, so all the tests are in the same method.
    Don't add new methods to this class - add more tests into test_query_works,
    or add a new test class.
    """

    def setup(self):
        super(TestExternalSearchWithWorks, self).setup()

        def _work(*args, **kwargs):
            """Convenience method to create a work with a license pool
            in the default collection.
            """
            return self._work(
                *args, with_license_pool=True, 
                collection=self._default_collection, **kwargs
            )

        if self.search:

            self.moby_dick = _work(title="Moby Dick", authors="Herman Melville", fiction=True)
            self.moby_dick.presentation_edition.subtitle = "Or, the Whale"
            self.moby_dick.presentation_edition.series = "Classics"
            self.moby_dick.summary_text = "Ishmael"
            self.moby_dick.presentation_edition.publisher = "Project Gutenberg"
            self.moby_dick.set_presentation_ready()

            self.moby_duck = _work(title="Moby Duck", authors="Donovan Hohn", fiction=False)
            self.moby_duck.presentation_edition.subtitle = "The True Story of 28,800 Bath Toys Lost at Sea"
            self.moby_duck.summary_text = "A compulsively readable narrative"
            self.moby_duck.presentation_edition.publisher = "Penguin"
            self.moby_duck.set_presentation_ready()

            self.title_match = _work(title="Match")
            self.title_match.set_presentation_ready()

            self.subtitle_match = _work()
            self.subtitle_match.presentation_edition.subtitle = "Match"
            self.subtitle_match.set_presentation_ready()

            self.summary_match = _work()
            self.summary_match.summary_text = "Match"
            self.summary_match.set_presentation_ready()
        
            self.publisher_match = _work()
            self.publisher_match.presentation_edition.publisher = "Match"
            self.publisher_match.set_presentation_ready()

            self.tess = _work(title="Tess of the d'Urbervilles")
            self.tess.set_presentation_ready()

            self.tiffany = _work(title="Breakfast at Tiffany's")
            self.tiffany.set_presentation_ready()
            
            self.les_mis = _work()
            self.les_mis.presentation_edition.title = u"Les Mis\u00E9rables"
            self.les_mis.set_presentation_ready()

            self.lincoln = _work(genre="Biography & Memoir", title="Abraham Lincoln")
            self.lincoln.set_presentation_ready()

            self.washington = _work(genre="Biography", title="George Washington")
            self.washington.set_presentation_ready()

            self.lincoln_vampire = _work(title="Abraham Lincoln: Vampire Hunter", genre="Fantasy")
            self.lincoln_vampire.set_presentation_ready()

            self.children_work = _work(title="Alice in Wonderland", audience=Classifier.AUDIENCE_CHILDREN)
            self.children_work.set_presentation_ready()

            self.ya_work = _work(title="Go Ask Alice", audience=Classifier.AUDIENCE_YOUNG_ADULT)
            self.ya_work.set_presentation_ready()

            self.adult_work = _work(title="Still Alice", audience=Classifier.AUDIENCE_ADULT)
            self.adult_work.set_presentation_ready()

            self.ya_romance = _work(audience=Classifier.AUDIENCE_YOUNG_ADULT, genre="Romance")
            self.ya_romance.set_presentation_ready()

            self.no_age = _work()
            self.no_age.summary_text = "President Barack Obama's election in 2008 energized the United States"
            self.no_age.set_presentation_ready()

            self.age_4_5 = _work()
            self.age_4_5.target_age = NumericRange(4, 5, '[]')
            self.age_4_5.summary_text = "President Barack Obama's election in 2008 energized the United States"
            self.age_4_5.set_presentation_ready()

            self.age_5_6 = _work(fiction=False)
            self.age_5_6.target_age = NumericRange(5, 6, '[]')
            self.age_5_6.set_presentation_ready()

            self.obama = _work(genre="Biography & Memoir")
            self.obama.target_age = NumericRange(8, 8, '[]')
            self.obama.summary_text = "President Barack Obama's election in 2008 energized the United States"
            self.obama.set_presentation_ready()

            self.dodger = _work()
            self.dodger.target_age = NumericRange(8, 8, '[]')
            self.dodger.summary_text = "Willie finds himself running for student council president"
            self.dodger.set_presentation_ready()

            self.age_9_10 = _work()
            self.age_9_10.target_age = NumericRange(9, 10, '[]')
            self.age_9_10.summary_text = "President Barack Obama's election in 2008 energized the United States"
            self.age_9_10.set_presentation_ready()

            self.age_2_10 = _work()
            self.age_2_10.target_age = NumericRange(2, 10, '[]')
            self.age_2_10.set_presentation_ready()

            self.pride = _work(title="Pride and Prejudice")
            self.pride.presentation_edition.medium = Edition.BOOK_MEDIUM
            self.pride.set_presentation_ready()

            self.pride_audio = _work(title="Pride and Prejudice")
            self.pride_audio.presentation_edition.medium = Edition.AUDIO_MEDIUM
            self.pride_audio.set_presentation_ready()

            self.sherlock = _work(
                title="The Adventures of Sherlock Holmes", 
                with_open_access_download=True
            )
            self.sherlock.presentation_edition.language = "en"
            self.sherlock.set_presentation_ready()

            self.sherlock_spanish = _work(title="Las Aventuras de Sherlock Holmes")
            self.sherlock_spanish.presentation_edition.language = "es"
            self.sherlock_spanish.set_presentation_ready()

            # Create a second collection that only contains a few books.
            self.tiny_collection = self._collection("A Tiny Collection")
            self.tiny_book = self._work(
                title="A Tiny Book", with_license_pool=True, 
                collection=self.tiny_collection
            )
            self.tiny_book.set_presentation_ready()

            # Both collections contain 'The Adventures of Sherlock
            # Holmes", but each collection licenses the book through a
            # different mechanism.
            self.sherlock_pool_2 = self._licensepool(
                edition=self.sherlock.presentation_edition,
                collection=self.tiny_collection
            )

            sherlock_2, is_new = self.sherlock_pool_2.calculate_work()
            eq_(self.sherlock, sherlock_2)
            eq_(2, len(self.sherlock.license_pools))
            time.sleep(2)

    def test_query_works(self):
        if not self.search:
            return

        # Convenience method to query the default library.
        def query(*args, **kwargs):
            return self.search.query_works(
                self._default_library, *args, **kwargs
            )
        

        # Pagination

        results = query("moby dick", None, None, None, None, None, None, None, size=1, offset=0)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.moby_dick.id), hits[0]["_id"])

        results = query("moby dick", None, None, None, None, None, None, None, size=1, offset=1)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.moby_duck.id), hits[0]["_id"])

        results = query("moby dick", None, None, None, None, None, None, None, size=2, offset=0)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.moby_dick.id), hits[0]["_id"])


        # Matches all main fields

        title_results = query("moby", None, None, None, None, None, None, None)
        eq_(2, len(title_results["hits"]["hits"]))

        author_results = query("melville", None, None, None, None, None, None, None)
        eq_(1, len(author_results["hits"]["hits"]))

        subtitle_results = query("whale", None, None, None, None, None, None, None)
        eq_(1, len(subtitle_results["hits"]["hits"]))

        series_results = query("classics", None, None, None, None, None, None, None)
        eq_(1, len(series_results["hits"]["hits"]))

        summary_results = query("ishmael", None, None, None, None, None, None, None)
        eq_(1, len(summary_results["hits"]["hits"]))

        publisher_results = query("gutenberg", None, None, None, None, None, None, None)
        eq_(1, len(summary_results["hits"]["hits"]))


        # Ranks title above subtitle above summary above publisher

        results = query("match", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(4, len(hits))
        eq_(unicode(self.title_match.id), hits[0]['_id'])
        eq_(unicode(self.subtitle_match.id), hits[1]['_id'])
        eq_(unicode(self.summary_match.id), hits[2]['_id'])
        eq_(unicode(self.publisher_match.id), hits[3]['_id'])
        

        # Ranks both title and author higher than only title

        results = query("moby melville", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.moby_dick.id), hits[0]['_id'])
        eq_(unicode(self.moby_duck.id), hits[1]['_id'])


        # Matches a quoted phrase

        results = query("\"moby dick\"", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.moby_dick.id), hits[0]["_id"])


        # Matches stemmed word

        results = query("runs", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.dodger.id), hits[0]['_id'])


        # Matches misspelled phrase

        results = query("movy", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))

        results = query("mleville", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))

        results = query("mo by dick", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))


        # Matches word with apostrophe

        results = query("durbervilles", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.tess.id), hits[0]['_id'])

        results = query("tiffanys", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.tiffany.id), hits[0]['_id'])


        # Matches work with unicode character

        results = query("les miserables", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.les_mis.id), hits[0]['_id'])


        # Matches fiction

        results = query("fiction moby", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.moby_dick.id), hits[0]['_id'])

        results = query("nonfiction moby", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.moby_duck.id), hits[0]['_id'])


        # Matches genre

        results = query("romance", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.ya_romance.id), hits[0]['_id'])


        # Matches audience

        results = query("children's", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.children_work.id), hits[0]['_id'])

        results = query("young adult", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        work_ids = sorted([unicode(self.ya_work.id), unicode(self.ya_romance.id)])
        result_ids = sorted([hit["_id"] for hit in hits])
        eq_(work_ids, result_ids)


        # Matches grade

        results = query("grade 4", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.age_9_10.id), hits[0]['_id'])
        
        results = query("grade 4-6", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.age_9_10.id), hits[0]['_id'])


        # Matches age

        results = query("age 9", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.age_9_10.id), hits[0]['_id'])
        
        results = query("age 10-12", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.age_9_10.id), hits[0]['_id'])


        # Ranks closest target age range highest

        results = query("age 3-5", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(3, len(hits))
        eq_(unicode(self.age_4_5.id), hits[0]['_id'])
        eq_(unicode(self.age_5_6.id), hits[1]['_id'])
        eq_(unicode(self.age_2_10.id), hits[2]['_id'])


        # Matches genre + audience

        results = query("young adult romance", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.ya_romance.id), hits[0]['_id'])


        # Matches age + fiction

        results = query("age 5 fiction", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.age_4_5.id), hits[0]['_id'])


        # Matches genre + title

        results = query("lincoln biography", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        eq_(unicode(self.lincoln.id), hits[0]['_id'])
        eq_(unicode(self.lincoln_vampire.id), hits[1]['_id'])


        # Matches age + genre + summary

        results = query("age 8 president biography", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(5, len(hits))
        eq_(unicode(self.obama.id), hits[0]['_id'])


        # Filters on media

        book_lane = Lane(self._db, self._default_library, "Books", media=Edition.BOOK_MEDIUM)
        audio_lane = Lane(self._db, self._default_library, "Audio", media=Edition.AUDIO_MEDIUM)

        results = query("pride and prejudice", book_lane.media, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.pride.id), hits[0]["_id"])

        results = query("pride and prejudice", audio_lane.media, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.pride_audio.id), hits[0]["_id"])


        # Filters on languages

        english_lane = Lane(self._db, self._default_library, "English", languages="en")
        spanish_lane = Lane(self._db, self._default_library, "Spanish", languages="es")
        both_lane = Lane(self._db, self._default_library, "Both", languages=["en", "es"])

        results = query("sherlock", None, english_lane.languages, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.sherlock.id), hits[0]["_id"])

        results = query("sherlock", None, spanish_lane.languages, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.sherlock_spanish.id), hits[0]["_id"])

        results = query("sherlock", None, both_lane.languages, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))


        # Filters on exclude languages

        no_english_lane = Lane(self._db, self._default_library, "English", exclude_languages="en")
        no_spanish_lane = Lane(self._db, self._default_library, "Spanish", exclude_languages="es")
        neither_lane = Lane(self._db, self._default_library, "Both", exclude_languages=["en", "es"])

        results = query("sherlock", None, None, no_english_lane.exclude_languages, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.sherlock_spanish.id), hits[0]["_id"])

        results = query("sherlock", None, None, no_spanish_lane.exclude_languages, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.sherlock.id), hits[0]["_id"])

        results = query("sherlock", None, None, neither_lane.exclude_languages, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(0, len(hits))
        

        # Filters on fiction

        fiction_lane = Lane(self._db, self._default_library, "fiction", fiction=True)
        nonfiction_lane = Lane(self._db, self._default_library, "nonfiction", fiction=False)
        both_lane = Lane(self._db, self._default_library, "both", fiction=Lane.BOTH_FICTION_AND_NONFICTION)

        results = query("moby dick", None, None, None, fiction_lane.fiction, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.moby_dick.id), hits[0]["_id"])

        results = query("moby dick", None, None, None, nonfiction_lane.fiction, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.moby_duck.id), hits[0]["_id"])

        results = query("moby dick", None, None, None, both_lane.fiction, None, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))


        # Filters on audience

        adult_lane = Lane(self._db, self._default_library, "Adult", audiences=Classifier.AUDIENCE_ADULT)
        ya_lane = Lane(self._db, self._default_library, "YA", audiences=Classifier.AUDIENCE_YOUNG_ADULT)
        children_lane = Lane(self._db, self._default_library, "Children", audiences=Classifier.AUDIENCE_CHILDREN)
        ya_and_children_lane = Lane(self._db, self._default_library, "YA and Children", audiences=[Classifier.AUDIENCE_YOUNG_ADULT, Classifier.AUDIENCE_CHILDREN])

        results = query("alice", None, None, None, None, adult_lane.audiences, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.adult_work.id), hits[0]["_id"])

        results = query("alice", None, None, None, None, ya_lane.audiences, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.ya_work.id), hits[0]["_id"])

        results = query("alice", None, None, None, None, children_lane.audiences, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.children_work.id), hits[0]["_id"])

        results = query("alice", None, None, None, None, ya_and_children_lane.audiences, None, None)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))
        work_ids = sorted([unicode(self.ya_work.id), unicode(self.children_work.id)])
        result_ids = sorted([hit["_id"] for hit in hits])
        eq_(work_ids, result_ids)


        # Filters on age range

        age_8_lane = Lane(self._db, self._default_library, "Age 8", age_range=[8, 8])
        age_5_8_lane = Lane(self._db, self._default_library, "Age 5-8", age_range=[5, 8])
        age_5_10_lane = Lane(self._db, self._default_library, "Age 5-10", age_range=[5, 10])
        age_8_10_lane = Lane(self._db, self._default_library, "Age 8-10", age_range=[8, 10])

        results = query("president", None, None, None, None, None, age_8_lane.age_range, None)
        hits = results["hits"]["hits"]
        eq_(3, len(hits))
        work_ids = sorted([unicode(self.no_age.id), unicode(self.obama.id), unicode(self.dodger.id)])
        result_ids = sorted([hit["_id"] for hit in hits])
        eq_(work_ids, result_ids)

        results = query("president", None, None, None, None, None, age_5_8_lane.age_range, None)
        hits = results["hits"]["hits"]
        eq_(4, len(hits))
        work_ids = sorted([unicode(self.no_age.id),
                           unicode(self.age_4_5.id),
                           unicode(self.obama.id),
                           unicode(self.dodger.id)])
        result_ids = sorted([hit["_id"] for hit in hits])
        eq_(work_ids, result_ids)

        results = query("president", None, None, None, None, None, age_5_10_lane.age_range, None)
        hits = results["hits"]["hits"]
        eq_(5, len(hits))
        work_ids = sorted([unicode(self.no_age.id),
                           unicode(self.age_4_5.id),
                           unicode(self.obama.id),
                           unicode(self.dodger.id),
                           unicode(self.age_9_10.id)])
        result_ids = sorted([hit["_id"] for hit in hits])
        eq_(work_ids, result_ids)

        results = query("president", None, None, None, None, None, age_8_10_lane.age_range, None)
        hits = results["hits"]["hits"]
        eq_(4, len(hits))
        work_ids = sorted([unicode(self.no_age.id),
                           unicode(self.obama.id),
                           unicode(self.dodger.id),
                           unicode(self.age_9_10.id)])
        result_ids = sorted([hit["_id"] for hit in hits])
        eq_(work_ids, result_ids)


        # Filters on genre

        biography_lane = Lane(self._db, self._default_library, "Biography", genres=["Biography & Memoir"])
        fantasy_lane = Lane(self._db, self._default_library, "Fantasy", genres=["Fantasy"])
        both_lane = Lane(self._db, self._default_library, "Both", genres=["Biography & Memoir", "Fantasy"], fiction=Lane.BOTH_FICTION_AND_NONFICTION)

        results = query("lincoln", None, None, None, None, None, None, biography_lane.genre_ids)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.lincoln.id), hits[0]["_id"])

        results = query("lincoln", None, None, None, None, None, None, fantasy_lane.genre_ids)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))
        eq_(unicode(self.lincoln_vampire.id), hits[0]["_id"])

        results = query("lincoln", None, None, None, None, None, None, both_lane.genre_ids)
        hits = results["hits"]["hits"]
        eq_(2, len(hits))

        # This query does not match anything because the book in
        # question is not in a collection associated with the default
        # library.
        results = query("a tiny book", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(0, len(hits))

        # If we don't pass in a library to query_works, the entire index is
        # searched and we can see everything regardless of which collection
        # it's in.
        results = self.search.query_works(
            None, "book", None, None, None, None, None, None, None
        )
        hits = results["hits"]["hits"]
        eq_(1, len(hits))        
        results = self.search.query_works(
            None, "moby dick", None, None, None, None, None, None, None
        )
        hits = results["hits"]["hits"]
        eq_(2, len(hits))        

        # If we add the missing collection to the default library, "A
        # Tiny Book" starts showing up in searches against that
        # library.
        self._default_library.collections.append(self.tiny_collection)
        results = query("a tiny book", None, None, None, None, None, None, None)
        hits = results["hits"]["hits"]
        eq_(1, len(hits))

        # Although the English edition of 'The Adventures of Sherlock
        # Holmes' is available through two different collections
        # associated with the default library, it only shows up once
        # in search results.
        results = query(
            "sherlock holmes", None, ['en'], None, None, 
            None, None, None
        )
        hits = results['hits']['hits']
        eq_(1, len(hits))
        [doc] = hits

        # When the second English LicensePool for 'The Adventures of
        # Sherlock Holmes' was associated with its Work, the Work was
        # automatically reindexed to incorporate with a new set of
        # collection IDs.
        collections = [x['collection_id'] for x in doc['_source']['collections']]
        expect_collections = [
            self.tiny_collection.id, self._default_collection.id
        ]
        eq_(set(collections), set(expect_collections))


class TestSearchQuery(DatabaseTest):
    def test_make_query(self):

        search = DummyExternalSearchIndex()

        # Basic query
        query = search.make_query("test")

        must = query['dis_max']['queries']

        eq_(3, len(must))
        stemmed_query = must[0]['simple_query_string']
        eq_("test", stemmed_query['query'])
        assert "title^4" in stemmed_query['fields']
        assert 'publisher' in stemmed_query['fields']

        phrase_queries = must[1]['bool']['should']
        eq_(3, len(phrase_queries))
        title_phrase_query = phrase_queries[0]['match_phrase']
        assert 'title.minimal' in title_phrase_query
        eq_("test", title_phrase_query['title.minimal'])

        # Query with fuzzy blacklist keyword
        query = search.make_query("basketball")

        must = query['dis_max']['queries']

        eq_(2, len(must))

        # Query with genre
        query = search.make_query("test romance")

        must = query['dis_max']['queries']

        eq_(4, len(must))
        full_query = must[0]['simple_query_string']
        eq_("test romance", full_query['query'])
        assert "title^4" in full_query['fields']
        assert 'publisher' in full_query['fields']

        classification_query = must[3]['bool']['must']
        eq_(2, len(classification_query))
        genre_query = classification_query[0]['match']
        assert 'genres.name' in genre_query
        eq_('Romance', genre_query['genres.name'])
        remaining_query = classification_query[1]['simple_query_string']
        assert "test" in remaining_query['query']
        assert "romance" not in remaining_query['query']
        assert 'author^4' in remaining_query['fields']


        # Query with fiction
        query = search.make_query("test nonfiction")
        
        must = query['dis_max']['queries']

        eq_(4, len(must))

        classification_query = must[3]['bool']['must']
        eq_(2, len(classification_query))
        fiction_query = classification_query[0]['match']
        assert 'fiction' in fiction_query
        eq_('Nonfiction', fiction_query['fiction'])
        remaining_query = classification_query[1]['simple_query_string']
        assert "test" in remaining_query['query']
        assert "fiction" not in remaining_query['query']
        assert 'author^4' in remaining_query['fields']
        

        # Query with genre and fiction
        query = search.make_query("test romance fiction")

        must = query['dis_max']['queries']

        eq_(4, len(must))

        classification_query = must[3]['bool']['must']
        eq_(3, len(classification_query))
        genre_query = classification_query[0]['match']
        assert 'genres.name' in genre_query
        eq_('Romance', genre_query['genres.name'])
        fiction_query = classification_query[1]['match']
        assert 'fiction' in fiction_query
        eq_('Fiction', fiction_query['fiction'])
        remaining_query = classification_query[2]['simple_query_string']
        assert "test" in remaining_query['query']
        assert "romance" not in remaining_query['query']
        assert "fiction" not in remaining_query['query']
        assert 'author^4' in remaining_query['fields']

        # Query with audience
        query = search.make_query("test young adult")

        must = query['dis_max']['queries']

        eq_(4, len(must))
        full_query = must[0]['simple_query_string']
        eq_("test young adult", full_query['query'])

        classification_query = must[3]['bool']['must']
        eq_(2, len(classification_query))
        audience_query = classification_query[0]['match']
        assert 'audience' in audience_query
        eq_('YoungAdult', audience_query['audience'])
        remaining_query = classification_query[1]['simple_query_string']
        assert "test" in remaining_query['query']
        assert "young" not in remaining_query['query']
        
        # Query with grade
        query = search.make_query("test grade 6")
        
        must = query['dis_max']['queries']

        eq_(4, len(must))
        full_query = must[0]['simple_query_string']
        eq_("test grade 6", full_query['query'])

        classification_query = must[3]['bool']['must']
        eq_(2, len(classification_query))
        grade_query = classification_query[0]['bool']
        assert 'must' in grade_query
        assert 'should' in grade_query
        age_must = grade_query['must']
        eq_(2, len(age_must))
        eq_(11, age_must[0]['range']['target_age.upper']['gte'])
        eq_(11, age_must[1]['range']['target_age.lower']['lte'])

        remaining_query = classification_query[1]['simple_query_string']
        assert "test" in remaining_query['query']
        assert "grade" not in remaining_query['query']
        
        # Query with age
        query = search.make_query("test 5-10 years")

        must = query['dis_max']['queries']

        eq_(4, len(must))
        full_query = must[0]['simple_query_string']
        eq_("test 5-10 years", full_query['query'])

        classification_query = must[3]['bool']['must']
        eq_(2, len(classification_query))
        grade_query = classification_query[0]['bool']
        assert 'must' in grade_query
        assert 'should' in grade_query
        age_must = grade_query['must']
        eq_(2, len(age_must))
        eq_(5, age_must[0]['range']['target_age.upper']['gte'])
        eq_(10, age_must[1]['range']['target_age.lower']['lte'])

        remaining_query = classification_query[1]['simple_query_string']
        assert "test" in remaining_query['query']
        assert "5" not in remaining_query['query']
        assert "years" not in remaining_query['query']


class TestSearchFilterFromLane(DatabaseTest):

    def test_make_filter_handles_collection_id(self):
        search = DummyExternalSearchIndex()

        lane = Lane(
            self._db, self._default_library, "anything", 
        )
        collection_ids = [x.id for x in lane.library.collections]
        filter = search.make_filter(
            collection_ids,
            lane.media, lane.languages, lane.exclude_languages,
            lane.fiction, list(lane.audiences), lane.age_range,
            lane.genre_ids,
        )
        collection_filter, medium_filter = filter['and']
        eq_(collection_filter['terms'], dict(collection_id=collection_ids))
        
    def test_query_works_from_lane_definition_handles_age_range(self):
        search = DummyExternalSearchIndex()

        lane = Lane(
            self._db, self._default_library, "For Ages 5-10", 
            age_range=[5,10]
        )
        filter = search.make_filter(
            [self._default_collection.id],
            lane.media, lane.languages, lane.exclude_languages,
            lane.fiction, list(lane.audiences), lane.age_range,
            lane.genre_ids,
        )

        collection_filter, medium_filter, audience_filter, target_age_filter = filter['and']
        upper_filter, lower_filter = target_age_filter['and']
        expect_upper = {'or': [{'range': {'target_age.upper': {'gte': 5}}}, {'bool': {'must_not': {'exists': {'field': 'target_age.upper'}}}}]}
        expect_lower = {'or': [{'range': {'target_age.lower': {'lte': 10}}}, {'bool': {'must_not': {'exists': {'field': 'target_age.lower'}}}}]}
        eq_(expect_upper, upper_filter)
        eq_(expect_lower, lower_filter)

    def test_query_works_from_lane_definition_handles_languages(self):
        search = DummyExternalSearchIndex()

        lane = Lane(
            self._db, self._default_library, "english or spanish", 
            languages=set(['eng', 'spa']),
        )
        filter = search.make_filter(
            [self._default_collection.id],
            lane.media, lane.languages, lane.exclude_languages,
            lane.fiction, list(lane.audiences), lane.age_range,
            lane.genre_ids,
        )
        
        collection_filter, languages_filter, medium_filter = filter['and']
        expect_languages = ['eng', 'spa']
        assert 'terms' in languages_filter
        assert 'language' in languages_filter['terms']
        eq_(expect_languages, sorted(languages_filter['terms']['language']))

    def test_query_works_from_lane_definition_handles_exclude_languages(self):
        search = DummyExternalSearchIndex()

        lane = Lane(
            self._db, self._default_library, "Not english or spanish", 
            exclude_languages=set(['eng', 'spa']),
        )
        filter = search.make_filter(
            [self._default_collection.id],
            lane.media, lane.languages, lane.exclude_languages,
            lane.fiction, list(lane.audiences), lane.age_range,
            lane.genre_ids,
        )
        
        collection_filter, exclude_languages_filter, medium_filter = filter['and']
        expect_exclude_languages = ['eng', 'spa']
        assert 'not' in exclude_languages_filter
        assert 'terms' in exclude_languages_filter['not']
        assert 'language' in exclude_languages_filter['not']['terms']
        eq_(expect_exclude_languages, sorted(exclude_languages_filter['not']['terms']['language']))

class TestSearchErrors(ExternalSearchTest):

    def test_search_connection_timeout(self):
        if not self.search:
            return

        attempts = []

        def bulk_with_timeout(docs, raise_on_error=False, raise_on_exception=False):
            attempts.append(docs)
            def error(doc):
                return dict(index=dict(status='TIMEOUT',
                                       exception='ConnectionTimeout',
                                       error='Connection Timeout!',
                                       _id=doc['_id'],
                                       data=doc))

            errors = map(error, docs)
            return 0, errors

        self.search.bulk = bulk_with_timeout
        
        work = self._work()
        successes, failures = self.search.bulk_update([work])
        eq_([], successes)
        eq_(1, len(failures))
        eq_(work, failures[0][0])
        eq_("Connection Timeout!", failures[0][1])

        # When all the documents fail, it tries again once with the same arguments.
        eq_([work.id, work.id],
            [docs[0]['_id'] for docs in attempts])

    def test_search_single_document_error(self):
        if not self.search:
            return

        successful_work = self._work()
        failing_work = self._work()
        
        def bulk_with_error(docs, raise_on_error=False, raise_on_exception=False):
            failures = [dict(data=dict(_id=failing_work.id),
                             error="There was an error!",
                             exception="Exception")]
            success_count = 1
            return success_count, failures

        self.search.bulk = bulk_with_error

        successes, failures = self.search.bulk_update([successful_work, failing_work])
        eq_([successful_work], successes)
        eq_(1, len(failures))
        eq_(failing_work, failures[0][0])
        eq_("There was an error!", failures[0][1])


class TestSearchIndexCoverageProvider(DatabaseTest):

    def test_operation(self):
        index = DummyExternalSearchIndex()
        provider = SearchIndexCoverageProvider(
            self._db, search_index_client=index
        )
        eq_(ExternalSearchIndex.search_index_update_operation(self._db),
            provider.operation)

    def test_success(self):
        work = self._work()
        index = DummyExternalSearchIndex()
        provider = SearchIndexCoverageProvider(
            self._db, search_index_client=index
        )
        results = provider.process_batch([work])

        # We got one success and no failures.
        eq_([work], results)

        # The work was added to the search index.
        eq_(1, len(index.docs))

    def test_failure(self):
        class DoomedExternalSearchIndex(DummyExternalSearchIndex):
            """All documents sent to this index will fail."""
            def bulk(self, docs, **kwargs):                
                return 0, [
                    dict(data=dict(_id=failing_work['_id']),
                         error="There was an error!",
                         exception="Exception")
                    for failing_work in docs
                ]

        work = self._work()
        index = DoomedExternalSearchIndex()
        provider = SearchIndexCoverageProvider(
            self._db, search_index_client=index
        )
        results = provider.process_batch([work])

        # We have one transient failure.
        [record] = results
        eq_(work, record.obj)
        eq_(True, record.transient)
        eq_('There was an error!', record.exception)
