"""Pending-request tables for sid+id rewriting (corr-3, corr-4)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PendingZedReq:
    """Zed sent us a request; we forwarded a (rewritten) one to child.

    Stored under the rewritten child-side jsonrpc id; on child's response
    we look up to recover Zed's original id.
    """

    zed_id: int
    method: str
    sent_at: float


@dataclass(frozen=True)
class PendingChildReq:
    """Child sent us a request; we forwarded it to Zed.

    Stored under the rewritten Zed-side jsonrpc id; on Zed's response we
    look up to recover child's original id.
    """

    child_id: int
    method: str
    sent_at: float


@dataclass
class PendingTables:
    """Symmetric pending-request tables for one ChildSession.

    (corr-3) On child death:
      - For every entry in `out_to_child`: synthesize JSON-RPC error to
        Zed using `zed_id`. Zed is blocking on this id.
      - For every entry in `out_to_zed`: drop. Log warning. When Zed's
        response eventually arrives, drop it too.

    (corr-4) Notifications never touch these tables — caller must
    classify the message as request/response/notification/error first.

    (corr-15) `promptQueueing: true` means N>1 entries with
    method="session/prompt" for the same sid may coexist. The tables
    are keyed by jsonrpc id (unique per request), so this works
    structurally — but no caller may assume "one pending prompt per
    session" anywhere.
    """

    out_to_child: dict[int, PendingZedReq] = field(default_factory=dict)
    out_to_zed: dict[int, PendingChildReq] = field(default_factory=dict)

    def record_zed_request(
        self, child_side_id: int, *, zed_id: int, method: str
    ) -> None:
        self.out_to_child[child_side_id] = PendingZedReq(
            zed_id=zed_id, method=method, sent_at=time.monotonic()
        )

    def record_child_request(
        self, zed_side_id: int, *, child_id: int, method: str
    ) -> None:
        self.out_to_zed[zed_side_id] = PendingChildReq(
            child_id=child_id, method=method, sent_at=time.monotonic()
        )

    def take_zed_request(self, child_side_id: int) -> PendingZedReq | None:
        return self.out_to_child.pop(child_side_id, None)

    def take_child_request(self, zed_side_id: int) -> PendingChildReq | None:
        return self.out_to_zed.pop(zed_side_id, None)

    def drain_outstanding_zed_requests(self) -> list[PendingZedReq]:
        """Return all `out_to_child` entries and clear the map.

        Used on child death to synthesize errors back to Zed (corr-3).
        """
        items = list(self.out_to_child.values())
        self.out_to_child.clear()
        return items

    def drain_outstanding_child_requests(self) -> list[PendingChildReq]:
        """Return all `out_to_zed` entries and clear the map.

        Used on child death — caller logs and drops Zed's eventual
        responses to these (corr-3).
        """
        items = list(self.out_to_zed.values())
        self.out_to_zed.clear()
        return items
