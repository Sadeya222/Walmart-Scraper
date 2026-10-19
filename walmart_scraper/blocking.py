"""Silent block detection and result-set validation.

Walmart rarely returns an obvious error.  Under bot pressure it returns a 200
with a challenge page, an empty ``__NEXT_DATA__`` payload, or a category that
"successfully" renders with zero tiles.  Treating any of those as data would
overwrite a good dataset with an empty one, so every listing response is graded
here before a single item is trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .constants import BLOCKING_MARKERS, BLOCKING_STATUS_CODES
from .text import fold

__all__ = ["BlockingReport", "BlockingDetector", "SEVERITY_OK", "SEVERITY_SUSPECT", "SEVERITY_BLOCKED"]

SEVERITY_OK = "ok"
SEVERITY_SUSPECT = "suspect"
SEVERITY_BLOCKED = "blocked"

# A challenge page is normally tiny and dense with these markers; a real
# category page is hundreds of KB.  Both signals are combined below.
_CHALLENGE_HINTS = (
    "captcha", "robot", "unusual traffic", "access denied", "perimeterx",
    "datadome", "turnstile", "verify you are human", "attention required",
)


@dataclass
class BlockingReport:
    """Outcome of grading one listing response."""

    blocked: bool = False
    suspect: bool = False
    reasons: list[str] = field(default_factory=list)
    severity: str = SEVERITY_OK
    item_count: int = 0
    canary_found: bool | None = None

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.suspect

    def summary(self) -> str:
        if not self.reasons:
            return "ok"
        return "; ".join(self.reasons)


class BlockingDetector:
    """Grades listing responses against configurable thresholds.

    ``min_items`` is the "abnormally thin results" guard.  ``canary_ids`` are
    products that must appear on the page when the page is honest - a canary that
    vanishes while the page still renders is the classic silent-block signature.
    """

    def __init__(
        self,
        *,
        min_items: int = 5,
        min_body_bytes: int = 20_000,
        require_next_data: bool = True,
        markers: Sequence[str] = BLOCKING_MARKERS,
        challenge_min_body_bytes: int = 400_000,
    ) -> None:
        self.min_items = max(0, int(min_items))
        self.min_body_bytes = max(0, int(min_body_bytes))
        self.require_next_data = bool(require_next_data)
        self.markers = tuple(markers)
        self.challenge_min_body_bytes = int(challenge_min_body_bytes)

    # ------------------------------------------------------------------ #
    def grade(
        self,
        *,
        url: str,
        status: int,
        body: str | bytes,
        item_count: int | None = None,
        item_ids: Iterable[str] | None = None,
        canary_ids: Iterable[str] | None = None,
        payload_parsed: bool = True,
    ) -> BlockingReport:
        """Grade one listing response.  Never raises.

        ``payload_parsed`` says whether the embedded state JSON was successfully
        decoded.  A page with a valid payload and zero items is the normal end of
        pagination - crawlers always overshoot the last page by one - so it is
        reported as ``ok`` rather than as a block.  Zero items on a page whose
        payload did not parse is a block.
        """
        report = BlockingReport(item_count=item_count or 0)
        text = body if isinstance(body, str) else (body or b"").decode("utf-8", "replace")
        folded = fold(text)
        length = len((body or b"") if isinstance(body, bytes) else text.encode("utf-8", "replace"))

        if status in BLOCKING_STATUS_CODES:
            report.reasons.append(f"http_status_{status}")
            return self._finish(report, hard=True)
        if status >= 400:
            report.reasons.append(f"http_status_{status}")
            return self._finish(report, hard=True)

        if not text.strip():
            report.reasons.append("empty_response")
            return self._finish(report, hard=True)

        if length < self.min_body_bytes:
            report.reasons.append(f"thin_body_{length}b<{self.min_body_bytes}b")

        # Challenge markers on a large page are normal prose ("captcha" inside a
        # product title); on a small page they mean we were served a wall.
        hits = [marker for marker in self.markers if marker in folded]
        if hits:
            if length < self.challenge_min_body_bytes:
                report.reasons.append("challenge_markers:" + ",".join(sorted(hits)[:4]))
                return self._finish(report, hard=True)
            report.reasons.append("challenge_markers_on_full_page:" + ",".join(sorted(hits)[:2]))
            report.suspect = True

        if self.require_next_data and "__NEXT_DATA__" not in text:
            # Some Walmart layouts ship state in a different script id, so this
            # is a strong suspect signal rather than a hard block.
            report.reasons.append("missing_next_data")
            report.suspect = True

        if item_count is not None:
            report.item_count = int(item_count)
            if report.item_count == 0:
                healthy_page = (
                    payload_parsed
                    and not report.suspect
                    and "__NEXT_DATA__" in text
                    and length >= self.min_body_bytes
                )
                if healthy_page:
                    report.reasons.append("empty_page_end_of_pagination")
                    report.severity = SEVERITY_OK
                    return report
                report.reasons.append("zero_items")
                return self._finish(report, hard=True)
            if report.item_count < self.min_items:
                report.reasons.append(f"thin_results_{report.item_count}<{self.min_items}")
                report.suspect = True

        canaries = [str(canary) for canary in (canary_ids or ()) if str(canary).strip()]
        if canaries:
            found = {str(item) for item in (item_ids or ()) if str(item).strip()}
            present = [canary for canary in canaries if canary in found]
            report.canary_found = bool(present)
            if not present:
                report.reasons.append("canary_missing:" + ",".join(canaries[:3]))
                return self._finish(report, hard=True)

        report.severity = SEVERITY_SUSPECT if report.suspect else SEVERITY_OK
        return report

    # ------------------------------------------------------------------ #
    @staticmethod
    def _finish(report: BlockingReport, *, hard: bool) -> BlockingReport:
        report.blocked = hard
        report.severity = SEVERITY_BLOCKED if hard else SEVERITY_SUSPECT
        return report

    # ------------------------------------------------------------------ #
    @staticmethod
    def extract_next_data(html: str) -> dict[str, Any] | None:
        """Parse Walmart's embedded ``__NEXT_DATA__`` JSON blob, or ``None``.

        Uses a brace-matching scan rather than a greedy regex so a nested
        ``</script>`` inside a product description cannot truncate the payload.
        """
        marker = 'id="__NEXT_DATA__"'
        start = html.find(marker)
        if start == -1:
            return None
        brace = html.find("{", start)
        if brace == -1:
            return None
        depth = 0
        in_string = False
        escaped = False
        for index in range(brace, len(html)):
            char = html[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[brace : index + 1])
                    except json.JSONDecodeError:
                        return None
        return None


def looks_like_challenge_page(html: str) -> bool:
    """Cheap standalone check used by the retry middleware."""
    if not html:
        return True
    folded = fold(html)
    return any(hint in folded for hint in _CHALLENGE_HINTS) and len(html) < 400_000
