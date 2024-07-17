from sqlalchemy import and_, false
from sqlalchemy.orm import Session
from typing_extensions import Self

from palace.manager.feed.acquisition import OPDSAcquisitionFeed
from palace.manager.feed.annotator.admin import AdminAnnotator
from palace.manager.sqlalchemy.model.edition import Edition
from palace.manager.sqlalchemy.model.lane import Pagination
from palace.manager.sqlalchemy.model.library import Library
from palace.manager.sqlalchemy.model.licensing import LicensePool
from palace.manager.sqlalchemy.model.work import Work


class AdminFeed(OPDSAcquisitionFeed):
    @classmethod
    def suppressed(
        cls,
        _db: Session,
        library: Library,
        title: str,
        url: str,
        annotator: AdminAnnotator,
        pagination: Pagination | None = None,
    ) -> Self:
        _pagination = pagination or Pagination.default()

        q = (
            _db.query(Work)
            .join(LicensePool)
            .join(Edition)
            .filter(
                and_(
                    LicensePool.suppressed == false(),
                    LicensePool.superceded == false(),
                    Work.suppressed_for.contains(library),
                )
            )
            .order_by(Edition.sort_title)
        )
        works = _pagination.modify_database_query(_db, q).all()

        feed = cls(title, url, works, annotator, pagination=_pagination)
        feed.generate_feed()

        # Render a 'start' link
        top_level_title = annotator.top_level_title()
        start_uri = annotator.groups_url(None)

        feed.add_link(start_uri, rel="start", title=top_level_title)

        # Render an 'up' link, same as the 'start' link to indicate top-level feed
        feed.add_link(start_uri, rel="up", title=top_level_title)

        if len(works) > 0:
            # There are works in this list. Add a 'next' link.
            feed.add_link(
                href=annotator.suppressed_url(_pagination.next_page),
                rel="next",
            )

        if _pagination.offset > 0:
            feed.add_link(
                annotator.suppressed_url(_pagination.first_page),
                rel="first",
            )

        if previous_page := _pagination.previous_page:
            feed.add_link(
                annotator.suppressed_url(previous_page),
                rel="previous",
            )

        return feed
