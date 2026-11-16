"""Column types for the two Postgres extensions the schema uses.

`Vector` maps a pgvector `vector` column and `GeographyPoint` maps a PostGIS
`geography(Point, 4326)` column. Both send and read values as text, which the
database driver handles without any extension-specific setup.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Dialect, func
from sqlalchemy.types import UserDefinedType

# WGS 84, the coordinate system of GPS and of Google Places coordinates.
WGS84_SRID = 4326

_POINT_TEXT_PATTERN = re.compile(r"^POINT\((?P<longitude>\S+) (?P<latitude>\S+)\)$")


@dataclass(frozen=True)
class GeoPoint:
    """A position on Earth in WGS 84 degrees.

    Attributes:
        latitude: Degrees north of the equator, negative for south.
        longitude: Degrees east of Greenwich, negative for west.
    """

    latitude: float
    longitude: float


def _vector_to_text(value: list[float] | None) -> str | None:
    if value is None:
        return None
    return "[" + ",".join(str(float(component)) for component in value) + "]"


def _vector_from_text(value: str | None) -> list[float] | None:
    if value is None:
        return None
    return [float(component) for component in value.strip("[]").split(",")]


def _point_to_text(value: GeoPoint | None) -> str | None:
    if value is None:
        return None
    return f"SRID={WGS84_SRID};POINT({value.longitude!r} {value.latitude!r})"


def _point_from_text(value: str | None) -> GeoPoint | None:
    if value is None:
        return None
    match = _POINT_TEXT_PATTERN.match(value)
    if match is None:
        raise ValueError("The database returned a geography value that is not a point")
    return GeoPoint(latitude=float(match["latitude"]), longitude=float(match["longitude"]))


class Vector(UserDefinedType[list[float]]):
    """A pgvector `vector` column, read and written as a list of floats.

    Attributes:
        dimension: Number of components every stored vector must have, or
            `None` to leave the size to the database column.
    """

    cache_ok = True

    def __init__(self, dimension: int | None = None) -> None:
        """Create the type.

        Args:
            dimension: Number of components for the column. Migrations pass it
                to create a sized column. Models leave it out, because the
                size is fixed by the migration and enforced by the database.
        """
        self.dimension = dimension

    def get_col_spec(self, **kw: object) -> str:
        """Return the column type for DDL, e.g. `vector(1024)`."""
        if self.dimension is None:
            return "vector"
        return f"vector({self.dimension})"

    def bind_processor(self, dialect: Dialect) -> Callable[[list[float] | None], str | None]:
        """Return the function that turns a list of floats into pgvector's text form."""
        return _vector_to_text

    def result_processor(
        self, dialect: Dialect, coltype: object
    ) -> Callable[[str | None], list[float] | None]:
        """Return the function that parses pgvector's text form into a list of floats."""
        return _vector_from_text


class GeographyPoint(UserDefinedType[GeoPoint]):
    """A PostGIS `geography(Point, 4326)` column, read and written as a `GeoPoint`."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        """Return the column type for DDL."""
        return f"geography(Point, {WGS84_SRID})"

    def bind_processor(self, dialect: Dialect) -> Callable[[GeoPoint | None], str | None]:
        """Return the function that turns a `GeoPoint` into PostGIS's text form."""
        return _point_to_text

    def column_expression(self, column: ColumnElement[GeoPoint]) -> ColumnElement[GeoPoint]:
        """Select the column as text, because PostGIS otherwise returns a binary point."""
        return func.ST_AsText(column, type_=self)

    def result_processor(
        self, dialect: Dialect, coltype: object
    ) -> Callable[[str | None], GeoPoint | None]:
        """Return the function that parses PostGIS's point text into a `GeoPoint`."""
        return _point_from_text
