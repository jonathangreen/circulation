from flask_babel import lazy_gettext as _
from core.model import (
    ExternalIntegration
)
from core.external_search import ExternalSearchIndex
from tests.testing import ExternalSearchTest

from api.admin.controller.self_tests import SelfTestsController

class SearchServiceSelfTestsController(SelfTestsController, ExternalSearchTest):

    def __init__(self, manager):
        super(SearchServiceSelfTestsController, self).__init__(manager)
        self.type = _("search service")

    def process_search_service_self_tests(self, identifier):
        return self._manage_self_tests(identifier)

    def _find_protocol_class(self, integration):
        # There's only one possibility for search integrations.
        return ExternalSearchIndex, (None, self._db,)

    def look_up_by_id(self, identifier):
        return self.look_up_service_by_id(
            identifier,
            ExternalIntegration.ELASTICSEARCH,
            ExternalIntegration.SEARCH_GOAL
        )
