from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from core.model import Collection
from core.model.devicetokens import DeviceToken
from core.model.patron import Hold, Loan, Patron
from core.monitor import PatronSweepMonitor
from core.util.datetime_helpers import utc_now
from core.util.notifications import PushNotifications


class PatronActivitySyncNotificationScript(PatronSweepMonitor):
    """Find patrons with stale last_activity_sync timestamps, and also who have loans/holds
    and notify said patron devices to re-sync their data"""

    def __init__(
        self,
        _db: Session,
        collection: Collection | None = None,
        batch_size: int | None = None,
        notifications: PushNotifications | None = None,
    ) -> None:
        super().__init__(_db, collection, batch_size)
        self.notifications = notifications or PushNotifications(
            self.services.config.sitewide.base_url()
        )

    STALE_ACTIVITY_SYNC_DAYS = 2
    SERVICE_NAME: str | None = "Patron Activity Sync Notification"

    def item_query(self) -> Query:
        expired_sync = utc_now() - timedelta(days=self.STALE_ACTIVITY_SYNC_DAYS)
        query: Query = super().item_query()
        query = (
            query.outerjoin(Hold)
            .outerjoin(Loan)
            .outerjoin(DeviceToken)
            .filter(or_(Loan.id != None, Hold.id != None))
            .filter(DeviceToken.id != None)
            .filter(
                or_(
                    Patron._last_loan_activity_sync < expired_sync,
                    Patron._last_loan_activity_sync == None,
                )
            )
        )
        return query

    def process_items(self, items: list[Patron]) -> None:
        self.notifications.send_activity_sync_message(items)
