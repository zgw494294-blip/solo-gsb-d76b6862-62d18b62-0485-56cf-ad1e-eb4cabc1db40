"""Plan the offline HTTP fetches needed to download a reconciled live window.

The reconciled timeline only describes *which* segments exist; this module
decides *what to download*:

* a segment without an ``EXT-X-BYTERANGE`` references the **whole** resource at
  its absolute URI — a single plain GET for that URI also supplies every other
  (byte-ranged) segment sharing the same URI;
* otherwise the segment occupies ``[offset, offset + length)`` bytes of the
  resource at its URI; when no segment of that URI references the whole
  resource, the half-open byte intervals are coalesced per URI, merging any two
  that overlap or are immediately adjacent (``next_start <= prev_end``), and
  one ``Range: bytes=start-end`` request is produced per merged interval.

Like every other endpoint the planner runs fully offline: it returns the
requests a caller *would* issue, never issuing them itself. Requests are
numbered in first-touch order (the sequence number of the first segment mapped
to them); each segment is mapped back to the request that carries it, together
with its start byte inside the response body and its length.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .reconcile import ReconcileError, ReconciledSegment, reconcile
from .vod import reject_unsupported_tags


@dataclass(frozen=True)
class PlannedRequest:
    """One HTTP GET to issue.

    ``range_header`` is ``None`` for a complete-resource request and a ready to
    send ``bytes=start-end`` value (inclusive end) for a byte-range request.
    """

    index: int
    uri: str
    range_header: Optional[str]


@dataclass(frozen=True)
class SegmentFetch:
    """Where one reconciled segment lives inside a planned response body."""

    sequence: int
    request_index: int
    # Byte offset of this segment inside the response body; None for a segment
    # that consumes the complete resource (the body starts at its byte zero).
    body_offset: Optional[int]
    # Bytes this segment occupies; None for a complete-resource segment.
    length: Optional[int]


@dataclass(frozen=True)
class FetchPlan:
    requests: tuple[PlannedRequest, ...]
    segments: tuple[SegmentFetch, ...]


def _http_uri(uri: str) -> bool:
    parsed = urlsplit(uri)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Coalesce half-open intervals that overlap or touch (adjacent) on disk."""

    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            # Adjacent: [a, b) + [b, c) -> [a, c); overlaps extend the tail.
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def plan_fetches(
    playlist_url: str,
    snapshots: Iterable[str],
    timeline: Optional[list[ReconciledSegment]] = None,
) -> FetchPlan:
    """Build the fetch plan for ``snapshots``.

    All merge errors from :func:`reconcile` propagate unchanged; non-HTTP(S)
    segment URIs and snapshots carrying ``#EXT-X-KEY`` / ``#EXT-X-MAP`` raise
    :class:`ReconcileError` (mapped to 422).
    """

    snapshot_list = list(snapshots)
    # Encryption keys and init-segment maps would add extra resources (key /
    # init segment) that the simple per-URI plan below cannot represent.
    reject_unsupported_tags(
        snapshot_list, operation="used to build an offline fetch plan"
    )

    if timeline is None:
        timeline = reconcile(playlist_url, snapshot_list)

    # Every segment URI must be an absolute http(s) URL. The parser resolves
    # relative URIs against playlist_url, so a non-http(s) result can only come
    # from a non-http(s) playlist base; this is the segment URI rule proper.
    for seg in timeline:
        if not _http_uri(seg.uri):
            raise ReconcileError(
                "unsupported_uri",
                (
                    f"segment at sequence {seg.sequence} has a non-http(s) URI "
                    f"{seg.uri!r}; only absolute http(s) segment URIs can be fetched"
                ),
                sequence=seg.sequence,
            )

    # Group segment references by their exact absolute URI.
    full_uris: set[str] = set()
    ranged: dict[str, list[tuple[int, int]]] = {}
    for seg in timeline:
        if seg.byte_offset is None:
            full_uris.add(seg.uri)
        else:
            end = seg.byte_offset + seg.byte_length
            ranged.setdefault(seg.uri, []).append((seg.byte_offset, end))

    # One request spec per URI: the complete resource when referenced in full,
    # otherwise one spec per merged byte interval (sorted by start). Indices
    # are assigned later, in first-touch order along the timeline.
    specs_by_uri: dict[str, list[tuple[str, Optional[str]]]] = {}
    for uri in full_uris:
        specs_by_uri[uri] = [(uri, None)]
    for uri, intervals in ranged.items():
        if uri in full_uris:
            continue  # the complete-resource request covers these segments too
        specs_by_uri[uri] = [
            (uri, f"bytes={start}-{end - 1}") for start, end in _merge_intervals(intervals)
        ]

    # Walk the timeline in sequence order: number requests on first touch and
    # map every segment into the response body of its request.
    by_spec: dict[int, PlannedRequest] = {}
    planned: list[PlannedRequest] = []
    segment_fetches: list[SegmentFetch] = []

    def _request_for(spec: tuple[str, Optional[str]]) -> PlannedRequest:
        request = by_spec.get(id(spec))
        if request is None:
            request = PlannedRequest(len(planned), spec[0], spec[1])
            by_spec[id(spec)] = request
            planned.append(request)
        return request

    for seg in timeline:
        specs = specs_by_uri[seg.uri]
        if seg.byte_offset is None or seg.uri in full_uris:
            request = _request_for(specs[0])  # full resource covers everything
            segment_fetches.append(
                SegmentFetch(
                    sequence=seg.sequence,
                    request_index=request.index,
                    body_offset=None,
                    length=None,
                )
            )
            continue

        end = seg.byte_offset + seg.byte_length
        spec = next(
            candidate
            for candidate in specs
            if candidate[1] is not None
            and _range_contains(candidate[1], seg.byte_offset, end)
        )
        request = _request_for(spec)
        range_start = int(request.range_header.split("=", 1)[1].split("-", 1)[0])
        segment_fetches.append(
            SegmentFetch(
                sequence=seg.sequence,
                request_index=request.index,
                body_offset=seg.byte_offset - range_start,
                length=seg.byte_length,
            )
        )

    return FetchPlan(requests=tuple(planned), segments=tuple(segment_fetches))


def _range_contains(range_header: str, start: int, end: int) -> bool:
    """Whether the half-open ``[start, end)`` fits inside a ``bytes=a-b`` range."""

    spec = range_header.split("=", 1)[1]
    range_start, range_end = spec.split("-", 1)
    return int(range_start) <= start and end - 1 <= int(range_end)
