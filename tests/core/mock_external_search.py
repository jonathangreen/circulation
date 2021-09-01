import contextlib
import logging

from core.external_search import ExternalSearchIndex, SortKeyPagination


@contextlib.contextmanager
def mock_search_index(mock=None):
    """Temporarily mock the ExternalSearchIndex implementation
    returned by the load() class method.
    """
    try:
        ExternalSearchIndex.MOCK_IMPLEMENTATION = mock
        yield mock
    finally:
        ExternalSearchIndex.MOCK_IMPLEMENTATION = None


class MockMeta(dict):
    """Mock the .meta object associated with an Elasticsearch search
    result.  This is necessary to get SortKeyPagination to work with
    MockExternalSearchIndex.
    """
    @property
    def sort(self):
        return self['_sort']


class MockExternalSearchIndex(ExternalSearchIndex):

    work_document_type = 'work-type'

    def __init__(self, url=None):
        self.url = url
        self.docs = {}
        self.works_index = "works"
        self.works_alias = "works-current"
        self.log = logging.getLogger("Mock external search index")
        self.queries = []
        self.search = list(self.docs.keys())
        self.test_search_term = "a search term"

    def _key(self, index, doc_type, id):
        return (index, doc_type, id)

    def index(self, index, doc_type, id, body):
        self.docs[self._key(index, doc_type, id)] = body
        self.search = list(self.docs.keys())

    def delete(self, index, doc_type, id):
        key = self._key(index, doc_type, id)
        if key in self.docs:
            del self.docs[key]

    def exists(self, index, doc_type, id):
        return self._key(index, doc_type, id) in self.docs

    def create_search_doc(self, query_string, filter=None, pagination=None, debug=False):
        return list(self.docs.values())

    def query_works(self, query_string, filter, pagination, debug=False):
        self.queries.append((query_string, filter, pagination, debug))
        # During a test we always sort works by the order in which the
        # work was created.

        def sort_key(x):
            # This needs to work with either a MockSearchResult or a
            # dictionary representing a raw search result.
            if isinstance(x, MockSearchResult):
                return x.work_id
            else:
                return x['_id']
        docs = sorted(list(self.docs.values()), key=sort_key)
        if pagination:
            start_at = 0
            if isinstance(pagination, SortKeyPagination):
                # Figure out where the previous page ended by looking
                # for the corresponding work ID.
                if pagination.last_item_on_previous_page:
                    look_for = pagination.last_item_on_previous_page[-1]
                    for i, x in enumerate(docs):
                        if x['_id'] == look_for:
                            start_at = i + 1
                            break
            else:
                start_at = pagination.offset
            stop = start_at + pagination.size
            docs = docs[start_at:stop]

        results = []
        for x in docs:
            if isinstance(x, MockSearchResult):
                results.append(x)
            else:
                results.append(
                    MockSearchResult(x["title"], x["author"], {}, x['_id'])
                )

        if pagination:
            pagination.page_loaded(results)
        return results

    def query_works_multi(self, queries, debug=False):
        # Implement query_works_multi by calling query_works several
        # times. This is the opposite of what happens in the
        # non-mocked ExternalSearchIndex, because it's easier to mock
        # the simple case and performance isn't an issue.
        for (query_string, filter, pagination) in queries:
            yield self.query_works(query_string, filter, pagination, debug)

    def count_works(self, filter):
        return len(self.docs)

    def bulk(self, docs, **kwargs):
        for doc in docs:
            self.index(doc['_index'], doc['_type'], doc['_id'], doc)
        return len(docs), []


class MockSearchResult(object):

    def __init__(self, sort_title, sort_author, meta, id):
        self.sort_title = sort_title
        self.sort_author = sort_author
        meta["id"] = id
        meta["_sort"] = [sort_title, sort_author, id]
        self.meta = MockMeta(meta)
        self.work_id = id

    def __contains__(self, k):
        return False

    def to_dict(self):
        return {
            "title": self.sort_title,
            "author": self.sort_author,
            "id": self.meta["id"],
            "meta": self.meta,
        }