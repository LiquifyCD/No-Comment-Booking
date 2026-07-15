from __future__ import annotations

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "licences": [item.as_dict() for item in self.licences],
            "examinationTypes": [item.as_dict() for item in self.examination_types],
            "locations": [item.as_dict() for item in self.locations],
        }


ID_KEYS = (
    "id",
    "value",
    "licenceId",
    "licenseId",
    "examinationTypeId",
    "locationId",
)
NAME_KEYS = (
    "name",
    "text",
    "label",
    "title",
    "description",
    "licenceName",
    "licenseName",
    "examinationTypeName",
    "locationName",
    "city",
    "languageKeyName",
)


def _named_lists(value: Any, names: set[str]) -> Iterable[list[Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.replace("_", "").replace("-", "").casefold()
            if normalized in names and isinstance(child, list):
                yield child
            yield from _named_lists(child, names)
    elif isinstance(value, list):
        for child in value:
            yield from _named_lists(child, names)


def _item(raw: Any, translations: dict[str, str]) -> CatalogItem | None:
    if not isinstance(raw, dict):
        return None
    raw_id = next((raw.get(key) for key in ID_KEYS if raw.get(key) not in (None, "")), None)
    raw_name = next(
        (raw.get(key) for key in NAME_KEYS if isinstance(raw.get(key), str) and raw[key].strip()),
        None,
    )
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if item_id <= 0 or not raw_name:
        return None
    name = translations.get(str(raw_name), str(raw_name)).strip()
    raw_description = raw.get("description") or raw.get("languageKeyDescription") or ""
    description = translations.get(str(raw_description), str(raw_description)).strip()
    return CatalogItem(item_id, name, description)


def _flatten_items(
    lists: Iterable[list[Any]], translations: dict[str, str]
) -> tuple[CatalogItem, ...]:
    by_id: dict[int, CatalogItem] = {}
    pending = list(lists)
    while pending:
        values = pending.pop()
        for raw in values:
            item = _item(raw, translations)
            if item:
                current = by_id.get(item.id)
                if current is None or item.name.casefold() < current.name.casefold():
                    by_id[item.id] = item
            if isinstance(raw, dict):
                pending.extend(child for child in raw.values() if isinstance(child, list))
    return tuple(sorted(by_id.values(), key=lambda item: (item.name.casefold(), item.id)))


def parse_booking_catalog(
    response: dict[str, Any], translations: dict[str, str] | None = None
) -> BookingCatalog:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ApiResponseError("search-information saknar ett giltigt data-objekt")

    translations = translations or {}
    licences = _flatten_items(
        _named_lists(data, {"licences", "licenses", "licenceoptions", "licenseoptions"}),
        translations,
    )
    examination_types = _flatten_items(
        _named_lists(data, {"examinationtypes", "examtypes", "tests", "testtypes"}),
        translations,
    )
    locations = _flatten_items(
        _named_lists(
            data,
            {
                "locations",
                "nearbylocations",
                "locationoptions",
                "cities",
                "testlocations",
            },
        ),
        translations,
    )
    if not (licences or examination_types or locations):
        raise ApiResponseError("Tjänsten returnerade ingen användbar bokningskatalog")
    return BookingCatalog(licences, examination_types, locations)


def parse_translations(response: dict[str, Any]) -> dict[str, str]:
    data = response.get("data")
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, list):
        raise ApiResponseError("Språksvaret saknar resources")
    result: dict[str, str] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        key = resource.get("key")
        value = resource.get("value")
        if isinstance(key, str) and isinstance(value, str) and key and value:
            result[key] = value
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
