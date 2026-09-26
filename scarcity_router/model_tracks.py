"""Model tracks: stable capability families over changing model slugs.

A track is a stable product/capability family (``openai/sol``,
``openai/luna``, ``openai/astra``) that persists across provider
generations; the physical slug (``gpt-6-sol``) remains the only execution
identity (D-053). The registry is a REVIEWED, versioned repository
artifact (`model-tracks.json`): slug patterns, classifications and
capability floors are owner-approved evidence, never runtime-derived and
never configurable at runtime. A server or worker loads the same
reviewed registry; neither can invent tracks.

Discipline:

- **Classification is structural and conservative.** A slug classifies
  into a track only when it full-matches the track's reviewed pattern;
  ambiguous matches (two patterns) fail closed; anything unmatched stays
  unclassified (DISCOVERED, never routable).
- **Floors are deliberately conservative.** A standard track's floor is
  the ONLY capability inheritance a newly discovered generation receives:
  one approval per track with dated provenance, never a copy of an older
  generation's full ratings. Stronger ratings arrive through explicit
  catalog evidence (D-025 governance) — exact catalog entries always
  take precedence over the floor.
- **Restricted tracks are never routable.** ``restricted`` marks families
  whose access is separately governed (e.g. ``daybreak``: security-review
  access): discovery never proves execution authorization, so restricted
  models are surfaced honestly in inventory but never materialized as
  executable resources.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from .gateway_validation import exact_shape, v_str
from .selection_types import CAPABILITY_DIMENSIONS, CONFIDENCE_VALUES

TRACK_REGISTRY_SCHEMA_VERSION = 1

#: Closed classification vocabulary.
TRACK_CLASSIFICATIONS: tuple[str, ...] = ("standard", "restricted")

_MAX_PATTERN = 256
_MAX_RATIONALE = 1500

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TrackRegistryError(ValueError):
    """The track registry artifact failed validation (fail closed)."""


class TrackFloor:
    """One approved conservative capability floor for a track.

    All six capability dimensions are stated explicitly (ratings 1-5) so
    a floor is a complete, reviewable assertion — never a partial merge
    with another model's ratings.
    """

    __slots__: tuple[str, ...] = (
        "ratings",
        "assessed_on",
        "confidence",
        "decision",
        "rationale",
        "hard_properties_continuity",
    )

    def __init__(
        self,
        ratings: dict[str, int],
        assessed_on: str,
        confidence: str,
        decision: str,
        rationale: str,
        hard_properties_continuity: dict[str, int] | None = None,
    ) -> None:
        missing = [dim for dim in CAPABILITY_DIMENSIONS if dim not in ratings]
        if missing:
            raise TrackRegistryError(f"track floor: missing dimensions {missing}")
        extra = [dim for dim in ratings if dim not in CAPABILITY_DIMENSIONS]
        if extra:
            raise TrackRegistryError(f"track floor: unknown dimensions {extra}")
        for dim in CAPABILITY_DIMENSIONS:
            value = ratings[dim]
            # The ``dict[str, int]`` annotation is not a runtime guarantee
            # for untrusted registry artifacts, so the redundant-looking
            # int check stays as a fail-closed runtime guard.
            if not isinstance(value, int) or isinstance(value, bool):  # pyright: ignore[reportUnnecessaryIsInstance] - runtime guard for untyped callers
                raise TrackRegistryError(f"track floor.{dim}: rating must be an integer")
            if not 1 <= value <= 5:
                raise TrackRegistryError(f"track floor.{dim}: rating {value} out of range")
        self.ratings: dict[str, int] = dict(ratings)
        if not _DATE_RE.match(assessed_on):
            raise TrackRegistryError("track floor.assessed_on: not a YYYY-MM-DD date")
        self.assessed_on: str = assessed_on
        if confidence not in CONFIDENCE_VALUES:
            raise TrackRegistryError(f"track floor.confidence: unknown {confidence!r}")
        self.confidence: str = confidence
        self.decision: str = v_str(decision, "track floor.decision")
        if len(rationale) > _MAX_RATIONALE:
            raise TrackRegistryError("track floor.rationale too long")
        self.rationale: str = rationale
        continuity: dict[str, int] = {}
        if hard_properties_continuity is not None:
            for key in ("input_context_tokens", "output_tokens"):
                value = hard_properties_continuity.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise TrackRegistryError(
                        f"track floor.hard_properties_continuity.{key}: "
                        + "positive integer required"
                    )
                continuity[key] = value
            unknown = set(hard_properties_continuity) - set(continuity)
            if unknown:
                raise TrackRegistryError(
                    f"track floor.hard_properties_continuity: unknown keys {sorted(unknown)}"
                )
        self.hard_properties_continuity: dict[str, int] | None = (
            continuity or None
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "ratings": dict(self.ratings),
            "assessed_on": self.assessed_on,
            "confidence": self.confidence,
            "decision": self.decision,
            "rationale": self.rationale,
        }
        if self.hard_properties_continuity is not None:
            out["hard_properties_continuity"] = dict(self.hard_properties_continuity)
        return out

    @classmethod
    def from_dict(cls, d: object) -> "TrackFloor":
        dd = exact_shape(
            d,
            ("ratings", "assessed_on", "confidence", "decision", "rationale"),
            ("hard_properties_continuity", "continuity_rationale"),
            "track_floor",
        )
        ratings_raw = dd["ratings"]
        if not isinstance(ratings_raw, dict):
            raise TrackRegistryError("track_floor.ratings must be an object")
        ratings: dict[str, int] = {}
        for dim, value in cast("dict[str, object]", ratings_raw).items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise TrackRegistryError(f"track_floor.ratings.{dim}: not an integer")
            ratings[dim] = value
        return cls(
            ratings=ratings,
            assessed_on=v_str(dd["assessed_on"], "track_floor.assessed_on"),
            confidence=v_str(dd["confidence"], "track_floor.confidence"),
            decision=v_str(dd["decision"], "track_floor.decision"),
            rationale=v_str(dd["rationale"], "track_floor.rationale"),
            hard_properties_continuity=(
                cast("dict[str, int]", dd["hard_properties_continuity"])
                if "hard_properties_continuity" in dd
                else None
            ),
        )


class ModelTrack:
    """One stable capability family: pattern, classification, floor."""

    __slots__: tuple[str, ...] = (
        "provider",
        "track",
        "display_name",
        "slug_pattern",
        "classification",
        "floor",
        "notes",
    )

    def __init__(
        self,
        provider: str,
        track: str,
        display_name: str,
        slug_pattern: str,
        classification: str,
        floor: TrackFloor | None,
        notes: str = "",
    ) -> None:
        from .gateway_validation import v_safe_id

        self.provider: str = v_safe_id(provider, "model_track.provider")
        self.track: str = v_safe_id(track, "model_track.track")
        if not display_name or len(display_name) > 128:
            raise TrackRegistryError("model_track.display_name: required, <= 128 chars")
        self.display_name: str = display_name
        if not slug_pattern or len(slug_pattern) > _MAX_PATTERN:
            raise TrackRegistryError("model_track.slug_pattern: required, <= 256 chars")
        try:
            self.slug_pattern: re.Pattern[str] = re.compile(slug_pattern)
        except re.error as exc:
            raise TrackRegistryError(
                f"model_track {provider}/{track}: invalid slug_pattern: {exc}"
            ) from None
        if classification not in TRACK_CLASSIFICATIONS:
            raise TrackRegistryError(
                f"model_track {provider}/{track}: unknown classification "
                + f"{classification!r}"
            )
        self.classification: str = classification
        if classification == "standard" and floor is None:
            raise TrackRegistryError(
                f"model_track {provider}/{track}: standard tracks require a floor"
            )
        if classification == "restricted" and floor is not None:
            raise TrackRegistryError(
                f"model_track {provider}/{track}: restricted tracks never route "
                + "and never assert a capability floor"
            )
        self.floor: TrackFloor | None = floor
        if len(notes) > _MAX_RATIONALE:
            raise TrackRegistryError("model_track.notes too long")
        self.notes: str = notes

    def matches(self, slug: str) -> bool:
        return self.slug_pattern.fullmatch(slug) is not None

    def track_id(self) -> str:
        return f"{self.provider}/{self.track}"

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "provider": self.provider,
            "track": self.track,
            "display_name": self.display_name,
            "slug_pattern": self.slug_pattern.pattern,
            "classification": self.classification,
        }
        if self.floor is not None:
            out["floor"] = self.floor.to_dict()
        if self.notes:
            out["notes"] = self.notes
        return out

    @classmethod
    def from_dict(cls, d: object) -> "ModelTrack":
        dd = exact_shape(
            d,
            (
                "provider",
                "track",
                "display_name",
                "slug_pattern",
                "classification",
            ),
            ("floor", "notes"),
            "model_track",
        )
        floor_raw = dd.get("floor")
        return cls(
            provider=v_str(dd["provider"], "model_track.provider"),
            track=v_str(dd["track"], "model_track.track"),
            display_name=v_str(dd["display_name"], "model_track.display_name"),
            slug_pattern=v_str(dd["slug_pattern"], "model_track.slug_pattern"),
            classification=v_str(dd["classification"], "model_track.classification"),
            floor=(
                TrackFloor.from_dict(cast("object", floor_raw))
                if isinstance(floor_raw, dict)
                else None
            ),
            notes=(
                v_str(dd["notes"], "model_track.notes") if "notes" in dd else ""
            ),
        )


class TrackRegistry:
    """The reviewed track set (loaded once from the repository artifact)."""

    __slots__: tuple[str, ...] = ("tracks",)

    def __init__(self, tracks: tuple[ModelTrack, ...]) -> None:
        ids = [track.track_id() for track in tracks]
        if len(set(ids)) != len(ids):
            raise TrackRegistryError(f"track registry: duplicate track ids {ids}")
        self.tracks: tuple[ModelTrack, ...] = tuple(tracks)

    def classify(self, provider: str, slug: str) -> ModelTrack | None:
        """The one track whose reviewed pattern matches this slug.

        Zero matches -> ``None`` (unclassified); more than one match is a
        registry defect and fails closed rather than picking a winner.
        """
        matches = [
            track
            for track in self.tracks
            if track.provider == provider and track.matches(slug)
        ]
        if len(matches) > 1:
            raise TrackRegistryError(
                f"track registry: {provider}/{slug} matches multiple tracks: "
                + ", ".join(track.track_id() for track in matches)
            )
        return matches[0] if matches else None

    def track_by_id(self, provider: str, track: str) -> ModelTrack | None:
        for candidate in self.tracks:
            if candidate.provider == provider and candidate.track == track:
                return candidate
        return None

    @classmethod
    def from_dict(cls, d: object) -> "TrackRegistry":
        dd = exact_shape(
            d,
            ("schema_version", "tracks"),
            ("updated_on", "description"),
            "track_registry",
        )
        version = dd["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise TrackRegistryError("track_registry.schema_version must be an integer")
        if version != TRACK_REGISTRY_SCHEMA_VERSION:
            raise TrackRegistryError(
                f"track_registry.schema_version {version} is not supported "
                + f"(expected {TRACK_REGISTRY_SCHEMA_VERSION})"
            )
        tracks_raw = dd["tracks"]
        if not isinstance(tracks_raw, list):
            raise TrackRegistryError("track_registry.tracks must be an array")
        return cls(
            tuple(
                ModelTrack.from_dict(item)
                for item in cast("list[object]", tracks_raw)
            )
        )


def load_track_registry(path: Path | None = None) -> TrackRegistry:
    """Load the reviewed registry artifact (repository root by default)."""
    resolved = path if path is not None else _default_registry_path()
    try:
        document = cast("object", json.loads(resolved.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise TrackRegistryError(f"track registry {resolved}: unreadable: {exc}") from None
    return TrackRegistry.from_dict(document)


def _default_registry_path() -> Path:
    from .selection_app import resolve_default_artifact

    return resolve_default_artifact("model-tracks.json")


__all__ = [
    "ModelTrack",
    "TRACK_CLASSIFICATIONS",
    "TRACK_REGISTRY_SCHEMA_VERSION",
    "TrackFloor",
    "TrackRegistry",
    "TrackRegistryError",
    "load_track_registry",
]
