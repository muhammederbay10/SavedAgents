"""The `place` content type: a restaurant, café, bar, shop or attraction to visit."""

from enum import StrEnum

from pydantic import Field

from app.types.base import (
    ContentType,
    EntityExtraction,
    EntityMention,
    FilterKind,
    NonEmptyText,
    SearchFilter,
    SubtypeQuestion,
    TagQuestion,
    values_of,
)

GOOGLE_PLACES_RESOLVER = "google_places"


class PlaceSubtype(StrEnum):
    """The kinds of place a post can be about."""

    RESTAURANT = "restaurant"
    CAFE = "cafe"
    BAR = "bar"
    SHOP = "shop"
    ATTRACTION = "attraction"


class Cuisine(StrEnum):
    """The cuisines a place can be filtered by. `OTHER` covers everything else."""

    TURKISH = "turkish"
    KEBAB = "kebab"
    SEAFOOD = "seafood"
    MEZE = "meze"
    MEDITERRANEAN = "mediterranean"
    MIDDLE_EASTERN = "middle eastern"
    ITALIAN = "italian"
    PIZZA = "pizza"
    BURGER = "burger"
    STEAKHOUSE = "steakhouse"
    JAPANESE = "japanese"
    CHINESE = "chinese"
    KOREAN = "korean"
    INDIAN = "indian"
    MEXICAN = "mexican"
    STREET_FOOD = "street food"
    VEGETARIAN = "vegetarian"
    OTHER = "other"


class PriceLevel(StrEnum):
    """How expensive a place is, following Google Places' price levels."""

    INEXPENSIVE = "inexpensive"
    MODERATE = "moderate"
    EXPENSIVE = "expensive"
    VERY_EXPENSIVE = "very expensive"


class IstanbulDistrict(StrEnum):
    """The 39 districts (*ilçe*) of Istanbul."""

    ADALAR = "Adalar"
    ARNAVUTKOY = "Arnavutköy"
    ATASEHIR = "Ataşehir"
    AVCILAR = "Avcılar"
    BAGCILAR = "Bağcılar"
    BAHCELIEVLER = "Bahçelievler"
    BAKIRKOY = "Bakırköy"
    BASAKSEHIR = "Başakşehir"
    BAYRAMPASA = "Bayrampaşa"
    BESIKTAS = "Beşiktaş"
    BEYKOZ = "Beykoz"
    BEYLIKDUZU = "Beylikdüzü"
    BEYOGLU = "Beyoğlu"
    BUYUKCEKMECE = "Büyükçekmece"
    CATALCA = "Çatalca"
    CEKMEKOY = "Çekmeköy"
    ESENLER = "Esenler"
    ESENYURT = "Esenyurt"
    EYUPSULTAN = "Eyüpsultan"
    FATIH = "Fatih"
    GAZIOSMANPASA = "Gaziosmanpaşa"
    GUNGOREN = "Güngören"
    KADIKOY = "Kadıköy"
    KAGITHANE = "Kağıthane"
    KARTAL = "Kartal"
    KUCUKCEKMECE = "Küçükçekmece"
    MALTEPE = "Maltepe"
    PENDIK = "Pendik"
    SANCAKTEPE = "Sancaktepe"
    SARIYER = "Sarıyer"
    SILIVRI = "Silivri"
    SULTANBEYLI = "Sultanbeyli"
    SULTANGAZI = "Sultangazi"
    SILE = "Şile"
    SISLI = "Şişli"
    TUZLA = "Tuzla"
    UMRANIYE = "Ümraniye"
    USKUDAR = "Üsküdar"
    ZEYTINBURNU = "Zeytinburnu"


class PlaceMention(EntityMention):
    """One venue a post mentions."""

    name: NonEmptyText = Field(
        description="The venue's name exactly as the post writes or says it."
    )
    area_hint: NonEmptyText | None = Field(
        description=(
            "The district, neighbourhood or city the post gives for the venue, "
            "e.g. 'Bebek' or 'Kadıköy Moda'. Null if the post does not say."
        )
    )
    cuisine: Cuisine | None = Field(
        description="The venue's cuisine. Null if it serves no food or the post is unclear."
    )


class PlaceExtraction(EntityExtraction):
    """What the text model extracts from a post about one or more places."""

    entities: tuple[PlaceMention, ...] = Field(
        description=(
            "Every venue the post mentions, in order. A list post has several. "
            "Empty if no venue is named."
        )
    )


PLACE = ContentType(
    name="place",
    description="A real-world venue to visit: a restaurant, café, bar, shop or attraction.",
    extraction_schema=PlaceExtraction,
    subtype_question=SubtypeQuestion(
        text="What kind of place is the post mainly about?",
        options=values_of(PlaceSubtype),
    ),
    tag_questions=(
        TagQuestion(
            tag="breakfast",
            text="Is the place shown or recommended for breakfast or brunch?",
        ),
        TagQuestion(
            tag="dessert",
            text="Is the post mainly about desserts, pastries or other sweets?",
        ),
        TagQuestion(tag="coffee", text="Is the post mainly about coffee?"),
        TagQuestion(
            tag="view",
            text="Does the post show or praise the place's view, such as the sea or the city?",
        ),
        TagQuestion(
            tag="outdoor",
            text="Does the place have a garden, terrace or rooftop to sit outside?",
        ),
    ),
    resolver=GOOGLE_PLACES_RESOLVER,
    search_filters=(
        SearchFilter(
            name="district",
            kind=FilterKind.CHOICE,
            description="The Istanbul district (ilçe) the place is in.",
            allowed_values=values_of(IstanbulDistrict),
        ),
        SearchFilter(
            name="near",
            kind=FilterKind.LOCATION,
            description="Places within a distance of a point, such as where the user is now.",
        ),
        SearchFilter(
            name="open_now",
            kind=FilterKind.BOOLEAN,
            description="True to keep only places that are open right now.",
        ),
        SearchFilter(
            name="cuisine",
            kind=FilterKind.CHOICE,
            description="The cuisine the place serves.",
            allowed_values=values_of(Cuisine),
        ),
        SearchFilter(
            name="price",
            kind=FilterKind.CHOICE,
            description="How expensive the place is.",
            allowed_values=values_of(PriceLevel),
        ),
    ),
)
