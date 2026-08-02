from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .engine import ApiResponseError


@dataclass(frozen=True)
class CatalogItem:
    id: int
    name: str
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "description": self.description}


@dataclass(frozen=True)
class BookingCatalog:
    licences: tuple[CatalogItem, ...]
    examination_types: tuple[CatalogItem, ...]
    locations: tuple[CatalogItem, ...]
    vehicle_types: tuple[CatalogItem, ...] = ()
    occasion_choices: tuple[CatalogItem, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "licences": [item.as_dict() for item in self.licences],
            "examinationTypes": [item.as_dict() for item in self.examination_types],
            "locations": [item.as_dict() for item in self.locations],
            "vehicleTypes": [item.as_dict() for item in self.vehicle_types],
            "occasionChoices": [item.as_dict() for item in self.occasion_choices],
        }


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9åäö]", "", str(value).casefold())


@dataclass(frozen=True)
class _CategorySpec:
    name: str
    container_hints: frozenset[str]
    id_keys: frozenset[str]
    name_keys: frozenset[str]


_SPECS = (
    _CategorySpec(
        "licences",
        frozenset({"licence", "license", "behörighet", "behörigheter"}),
        frozenset({"licenceid", "licenseid", "licencecategoryid", "licensecategoryid"}),
        frozenset({"licencename", "licensename", "licencetypename", "licensetypename"}),
    ),
    _CategorySpec(
        "examinationTypes",
        frozenset({"examination", "examtype", "testtype", "tests", "provtyp", "provtyper"}),
        frozenset({"examinationtypeid", "examtypeid", "testtypeid"}),
        frozenset({"examinationtypename", "examtypename", "testtypename"}),
    ),
    _CategorySpec(
        "locations",
        frozenset({"location", "locations", "city", "cities", "testlocation", "provort", "orter"}),
        frozenset({"locationid", "testlocationid", "cityid", "officeid"}),
        frozenset({"locationname", "testlocationname", "cityname", "officename"}),
    ),
    _CategorySpec(
        "vehicleTypes",
        frozenset(
            {
                "vehicle",
                "vehicletype",
                "vehicletypes",
                "transmission",
                "gearbox",
                "fordonstyp",
                "växellåda",
            }
        ),
        frozenset({"vehicletypeid", "transmissiontypeid", "gearboxtypeid"}),
        frozenset({"vehicletypename", "transmissiontypename", "gearboxtypename"}),
    ),
    _CategorySpec(
        "occasionChoices",
        frozenset({"occasion", "occasionchoice", "occasionchoices", "rental", "hirecar", "hyrbil"}),
        frozenset({"occasionchoiceid", "rentaloptionid", "hirecaroptionid"}),
        frozenset({"occasionchoicename", "rentaloptionname", "hirecaroptionname"}),
    ),
)

_GENERIC_ID_KEYS = ("id", "value", "key")
_GENERIC_NAME_KEYS = (
    "name",
    "text",
    "label",
    "title",
    "city",
    "languagekeyname",
    "description",
)
_DESCRIPTION_KEYS = ("description", "languagekeydescription", "helptext")


@dataclass(frozen=True)
class _Candidate:
    item: CatalogItem
    score: int


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    normalized = {_normalized(key): value for key, value in mapping.items()}
    for key in keys:
        value = normalized.get(key)
        if value not in (None, ""):
            return value
    return None


def _path_matches(path: tuple[str, ...], hints: frozenset[str]) -> bool:
    normalized_path = tuple(_normalized(part) for part in path)
    return any(any(hint in part for hint in hints) for part in normalized_path)


def _candidate(
    raw: dict[str, Any],
    path: tuple[str, ...],
    spec: _CategorySpec,
    translations: dict[str, str],
) -> _Candidate | None:
    explicit_id = _first(raw, spec.id_keys)
    explicit_name = _first(raw, spec.name_keys)
    hinted = _path_matches(path, spec.container_hints)
    raw_id = (
        explicit_id
        if explicit_id is not None
        else _first(raw, _GENERIC_ID_KEYS)
        if hinted
        else None
    )
    raw_name = (
        explicit_name
        if isinstance(explicit_name, str) and explicit_name.strip()
        else _first(raw, _GENERIC_NAME_KEYS)
        if hinted or explicit_id is not None
        else None
    )
    if raw_id is None or not isinstance(raw_name, str) or not raw_name.strip():
        return None
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if item_id <= 0:
        return None
    name = translations.get(raw_name, raw_name).strip()
    raw_description = _first(raw, _DESCRIPTION_KEYS)
    description = (
        translations.get(raw_description, raw_description).strip()
        if isinstance(raw_description, str)
        else ""
    )
    score = (
        (4 if explicit_id is not None else 0) + (3 if explicit_name else 0) + (2 if hinted else 0)
    )
    return _Candidate(CatalogItem(item_id, name, description), score)


def _records(value: Any) -> Iterable[tuple[dict[str, Any], tuple[str, ...]]]:
    def walk(child: Any, path: tuple[str, ...]) -> Iterable[tuple[dict[str, Any], tuple[str, ...]]]:
        if isinstance(child, dict):
            yield child, path
            for key, nested in child.items():
                if str(key).isdigit() and isinstance(nested, str) and nested.strip():
                    yield {"id": key, "name": nested}, path
                yield from walk(nested, (*path, str(key)))
        elif isinstance(child, list):
            for index, nested in enumerate(child):
                yield from walk(nested, (*path, str(index)))

    return walk(value, ("data",))


def parse_booking_catalog(
    response: dict[str, Any], translations: dict[str, str] | None = None
) -> BookingCatalog:
    data = response.get("data")
    if not isinstance(data, (dict, list)):
        raise ApiResponseError("API-svaret saknar indexerbar data")
    translations = translations or {}
    records = tuple(_records(data))
    indexed: dict[str, tuple[CatalogItem, ...]] = {}
    for spec in _SPECS:
        best: dict[int, _Candidate] = {}
        for raw, path in records:
            candidate = _candidate(raw, path, spec, translations)
            if candidate is None:
                continue
            current = best.get(candidate.item.id)
            if current is None or (candidate.score, candidate.item.name.casefold()) > (
                current.score,
                current.item.name.casefold(),
            ):
                best[candidate.item.id] = candidate
        indexed[spec.name] = tuple(
            sorted(
                (entry.item for entry in best.values()),
                key=lambda item: (item.name.casefold(), item.id),
            )
        )
    if not any(indexed.values()):
        raise ApiResponseError("API-svaret innehåller inga indexerbara bokningsalternativ")
    return BookingCatalog(
        indexed["licences"],
        indexed["examinationTypes"],
        indexed["locations"],
        indexed["vehicleTypes"],
        indexed["occasionChoices"],
    )


def parse_translations(response: dict[str, Any]) -> dict[str, str]:
    data = response.get("data")
    result: dict[str, str] = {}

    def collect(value: Any, *, resource_context: bool = False) -> None:
        if isinstance(value, dict):
            key = value.get("key") or value.get("resourceKey")
            text = value.get("value") or value.get("text") or value.get("translation")
            if isinstance(key, str) and isinstance(text, str) and key and text:
                result[key] = text
            if resource_context:
                for child_key, child in value.items():
                    if (
                        isinstance(child_key, str)
                        and isinstance(child, str)
                        and child_key
                        and child
                    ):
                        result.setdefault(child_key, child)
            for child_key, child in value.items():
                collect(
                    child,
                    resource_context=resource_context
                    or _normalized(child_key) in {"resources", "translations", "languageitems"},
                )
        elif isinstance(value, list):
            for child in value:
                collect(child, resource_context=resource_context)

    collect(data)
    if not result:
        raise ApiResponseError("Språksvaret innehåller inga översättningar")
    return result


def resolve_item_id(items: Iterable[CatalogItem], name: str) -> int:
    wanted = name.strip().casefold()
    matches = [item for item in items if item.name.casefold() == wanted]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        raise ApiResponseError(f"Ingen katalogpost matchar '{name}'")
    raise ApiResponseError(f"Flera katalogposter matchar '{name}'")
