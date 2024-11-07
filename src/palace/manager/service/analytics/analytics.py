from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import flask

from palace.manager.api.s3_analytics_provider import S3AnalyticsProvider
from palace.manager.core.local_analytics_provider import LocalAnalyticsProvider
from palace.manager.sqlalchemy.model.library import Library
from palace.manager.sqlalchemy.model.licensing import LicensePool
from palace.manager.sqlalchemy.model.patron import Patron
from palace.manager.util.datetime_helpers import utc_now
from palace.manager.util.log import LoggerMixin

if TYPE_CHECKING:
    from palace.manager.service.storage.s3 import S3Service


class Analytics(LoggerMixin):
    """Dispatches methods for analytics providers."""

    def __init__(
        self,
        s3_analytics_enabled: bool = False,
        s3_service: S3Service | None = None,
    ) -> None:
        self.providers = [LocalAnalyticsProvider()]

        if s3_analytics_enabled:
            if s3_service is not None:
                self.providers.append(S3AnalyticsProvider(s3_service))
            else:
                self.log.info(
                    "S3 analytics is not configured: No analytics bucket was specified."
                )

    def collect_event(
        self,
        library: Library,
        license_pool: LicensePool | None,
        event_type: str,
        time: datetime | None = None,
        patron: Patron | None = None,
        **kwargs: Any,
    ) -> None:
        if not time:
            time = utc_now()

        user_agent: str | None = None
        try:
            user_agent = flask.request.user_agent.string
            if user_agent == "":
                user_agent = None
        except Exception as e:
            self.log.warning(f"Unable to resolve the user_agent: {repr(e)}")

        for provider in self.providers:
            provider.collect_event(
                library,
                license_pool,
                event_type,
                time,
                user_agent=user_agent,
                patron=patron,
                **kwargs,
            )

    def is_configured(self) -> bool:
        return len(self.providers) > 0
