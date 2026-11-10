"""
Is a job location in the US? For the archive's "US only" filter. Plain code, no Claude.
Handles "Seattle, WA", "Madison, Wisconsin", "Texas", "NYC", "Remote in USA", and lists like
"San Diego, CA · Boxborough, MA +1 more"; "Toronto, ON, Canada" or "London, UK" aren't US.
"""

import re

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico",
}
NAMES = {n.lower() for n in STATES.values()} | {"washington dc", "washington, dc"}
US_WORDS = re.compile(r"\b(?:usa|u\.s\.a?\.?|united states|us)\b|^(?:nyc|sf|la|south sf|remote|hybrid)$", re.I)


def is_us_place(place: str) -> bool:
    place = re.sub(r"\s*\+\d+ more$", "", place.strip())
    last = place.rsplit(",", 1)[-1].strip().replace(".", "")          # "D.C." -> "DC"
    # A state code needs a comma before it ("Austin, TX") or to stand alone ("NJ"), so a city
    # named "LA" alone is caught by US_WORDS and not mistaken for Louisiana's code elsewhere.
    is_code = last.upper() in STATES and ("," in place or place.upper() == last.upper())
    return is_code or last.lower() in NAMES or bool(US_WORDS.search(place))


def is_us(location: str | None) -> bool | None:
    """True if any listed place is in the US, False if none is, None if there's no location."""
    places = [p for p in re.split(r"\s*[·;|]\s*", location or "") if p.strip()]
    if not places:
        return None
    return any(is_us_place(p) for p in places)
