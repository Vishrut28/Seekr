"""Worldwide place reference data for query parsing and indexing.

Reference data, not a guess about people: it lets "in Deutschland", "à
Genève" or "北京" become real place filters. Keys are folded with
textnorm.fold, so accents and case never matter.

Deliberately left out: place names that are also common first names or
surnames (Jordan, Chad, Florence, Victoria, Charlotte, Madison, Georgia...).
The parser checks places BEFORE names, so including them would silently turn
"find Jordan" into a country filter. They stay reachable as ordinary words.
"""

from __future__ import annotations

from .textnorm import fold

# ISO-3166 alpha-2 -> every common way of writing the country: English,
# endonyms, and frequent abbreviations.
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "AE": ("united arab emirates", "uae", "emirates"),
    "AR": ("argentina",),
    "AT": ("austria", "österreich"),
    "AU": ("australia",),
    "BD": ("bangladesh", "বাংলাদেশ"),
    "BE": ("belgium", "belgië", "belgique", "belgien"),
    "BG": ("bulgaria", "българия"),
    "BR": ("brazil", "brasil"),
    "CA": ("canada",),
    "CH": ("switzerland", "schweiz", "suisse", "svizzera"),
    "CL": ("chile",),
    "CN": ("china", "中国", "中國", "prc"),
    "CO": ("colombia",),
    "CR": ("costa rica",),
    "CY": ("cyprus",),
    "CZ": ("czech republic", "czechia", "česko", "česká republika"),
    "DE": ("germany", "deutschland"),
    "DK": ("denmark", "danmark"),
    "DZ": ("algeria",),
    "EC": ("ecuador",),
    "EE": ("estonia", "eesti"),
    "EG": ("egypt", "مصر"),
    "ES": ("spain", "españa"),
    "ET": ("ethiopia",),
    "FI": ("finland", "suomi"),
    "FR": ("france",),
    "GB": ("united kingdom", "uk", "britain", "great britain", "england",
           "scotland", "wales", "northern ireland"),
    "GH": ("ghana",),
    "GR": ("greece", "ελλάδα", "hellas"),
    "HK": ("hong kong", "香港"),
    "HR": ("croatia", "hrvatska"),
    "HU": ("hungary", "magyarország"),
    "ID": ("indonesia",),
    "IE": ("ireland", "éire"),
    "IL": ("israel", "ישראל"),
    "IN": ("india", "भारत", "bharat"),
    "IQ": ("iraq",),
    "IR": ("iran", "ایران"),
    "IS": ("iceland", "ísland"),
    "IT": ("italy", "italia"),
    "JP": ("japan", "日本", "nippon"),
    "KE": ("kenya",),
    "KR": ("south korea", "korea", "대한민국", "한국"),
    "KZ": ("kazakhstan",),
    "LK": ("sri lanka",),
    "LT": ("lithuania", "lietuva"),
    "LU": ("luxembourg",),
    "LV": ("latvia", "latvija"),
    "MA": ("morocco", "maroc"),
    "MX": ("mexico", "méxico"),
    "MY": ("malaysia",),
    "NG": ("nigeria",),
    "NL": ("netherlands", "the netherlands", "nederland"),
    "NO": ("norway", "norge"),
    "NP": ("nepal", "नेपाल"),
    "NZ": ("new zealand", "aotearoa"),
    "PE": ("peru", "perú"),
    "PH": ("philippines", "pilipinas"),
    "PK": ("pakistan", "پاکستان"),
    "PL": ("poland", "polska"),
    "PT": ("portugal",),
    "QA": ("qatar",),
    "RO": ("romania", "românia"),
    "RS": ("serbia", "srbija"),
    "RU": ("russia", "россия", "russian federation"),
    "SA": ("saudi arabia", "ksa", "السعودية"),
    "SE": ("sweden", "sverige"),
    "SG": ("singapore",),
    "SI": ("slovenia", "slovenija"),
    "SK": ("slovakia", "slovensko"),
    "TH": ("thailand", "ประเทศไทย"),
    "TN": ("tunisia",),
    "TR": ("turkey", "türkiye"),
    "TW": ("taiwan", "臺灣", "台灣", "台湾"),
    "UA": ("ukraine", "україна"),
    "US": ("united states", "united states of america", "usa", "america", "u.s.",
           "u.s.a."),
    "UY": ("uruguay",),
    "VE": ("venezuela",),
    "VN": ("vietnam", "viet nam", "việt nam"),
    "ZA": ("south africa",),
}

_DEMONYMS: dict[str, tuple[str, ...]] = {
    "AR": ("argentinian", "argentine"), "AT": ("austrian",), "AU": ("australian",),
    "BD": ("bangladeshi",), "BE": ("belgian",), "BR": ("brazilian",),
    "CA": ("canadian",), "CH": ("swiss",), "CL": ("chilean",), "CN": ("chinese",),
    "CO": ("colombian",), "CZ": ("czech",), "DE": ("german",), "DK": ("danish",),
    "EG": ("egyptian",), "ES": ("spanish",), "FI": ("finnish",), "FR": ("french",),
    "GB": ("british",), "GR": ("greek",), "HU": ("hungarian",), "ID": ("indonesian",),
    "IE": ("irish",), "IL": ("israeli",), "IN": ("indian",), "IR": ("iranian",),
    "IT": ("italian",), "JP": ("japanese",), "KE": ("kenyan",), "KR": ("korean",),
    "LK": ("sri lankan",), "MX": ("mexican",), "MY": ("malaysian",),
    "NG": ("nigerian",), "NL": ("dutch",), "NO": ("norwegian",), "NP": ("nepali",
    "nepalese"), "NZ": ("kiwi",), "PE": ("peruvian",), "PH": ("filipino",),
    "PK": ("pakistani",), "PL": ("polish",), "PT": ("portuguese",),
    "RO": ("romanian",), "RU": ("russian",), "SA": ("saudi",), "SE": ("swedish",),
    "SG": ("singaporean",), "TH": ("thai",), "TR": ("turkish",), "TW": ("taiwanese",),
    "UA": ("ukrainian",), "US": ("american",), "VN": ("vietnamese",),
    "ZA": ("south african",),
}

# (display name, country or None when ambiguous, alternate spellings...)
_CITIES: tuple[tuple, ...] = (
    # India
    ("Bengaluru", "IN", "bangalore"), ("Mumbai", "IN", "bombay"),
    ("Delhi", "IN", "new delhi"), ("Hyderabad", None), ("Chennai", "IN", "madras"),
    ("Pune", "IN", "poona"), ("Kolkata", "IN", "calcutta"), ("Ahmedabad", "IN"),
    ("Noida", "IN"), ("Gurugram", "IN", "gurgaon"), ("Jaipur", "IN"),
    ("Kochi", "IN", "cochin"), ("Thiruvananthapuram", "IN", "trivandrum"),
    ("Chandigarh", "IN"), ("Indore", "IN"), ("Bhubaneswar", "IN"),
    ("Coimbatore", "IN"), ("Lucknow", "IN"), ("Nagpur", "IN"), ("Mysuru", "IN", "mysore"),
    ("Visakhapatnam", "IN", "vizag"), ("Kanpur", "IN"), ("Surat", "IN"),
    ("Vadodara", "IN", "baroda"), ("Guwahati", "IN"), ("Mangaluru", "IN", "mangalore"),
    # United States
    ("California", "US"), ("Texas", "US"), ("Washington", None), ("New York", "US",
     "nyc", "new york city"), ("Florida", "US"), ("Illinois", "US"),
    ("Massachusetts", "US"), ("Colorado", "US"), ("Oregon", "US"), ("Nevada", "US"),
    ("Arizona", "US"), ("Pennsylvania", "US"), ("Ohio", "US"), ("Michigan", "US"),
    ("North Carolina", "US"), ("Virginia", "US"), ("New Jersey", "US"),
    ("Utah", "US"), ("Minnesota", "US"), ("Maryland", "US"),
    ("San Francisco", "US", "sf"), ("Los Angeles", "US"), ("Seattle", "US"),
    ("Austin", None), ("Boston", "US"), ("Chicago", "US"), ("Denver", "US"),
    ("Atlanta", "US"), ("Miami", "US"), ("San Jose", None), ("San Diego", "US"),
    ("Palo Alto", "US"), ("Mountain View", "US"), ("Menlo Park", "US"),
    ("Sunnyvale", "US"), ("Redmond", "US"), ("Pittsburgh", "US"),
    ("Philadelphia", "US"), ("Portland", None),
    ("Berkeley", "US"), ("Silicon Valley", "US", "bay area", "sf bay area"),
    # Canada, Latin America
    ("Toronto", "CA"), ("Vancouver", "CA"), ("Montreal", "CA", "montréal"),
    ("Ottawa", "CA"), ("Waterloo", None), ("Calgary", "CA"),
    ("Mexico City", "MX", "ciudad de mexico", "cdmx"), ("Guadalajara", "MX"),
    ("São Paulo", "BR", "sao paulo"), ("Rio de Janeiro", "BR"),
    ("Buenos Aires", "AR"), ("Bogotá", "CO"), ("Medellín", "CO"),
    # Europe
    ("London", "GB"), ("Cambridge", None), ("Oxford", "GB"), ("Manchester", "GB"),
    ("Edinburgh", "GB"), ("Bristol", "GB"), ("Dublin", "IE"),
    ("Paris", "FR"), ("Lyon", "FR"), ("Grenoble", "FR"), ("Toulouse", "FR"),
    ("Berlin", "DE"), ("Munich", "DE", "münchen", "muenchen"), ("Hamburg", "DE"),
    ("Frankfurt", "DE"), ("Cologne", "DE", "köln", "koeln"), ("Stuttgart", "DE"),
    ("Aachen", "DE"), ("Heidelberg", "DE"), ("Karlsruhe", "DE"), ("Darmstadt", "DE"),
    ("Tübingen", "DE", "tuebingen"), ("Saarbrücken", "DE", "saarbruecken"),
    ("Amsterdam", "NL"), ("Delft", "NL"), ("Eindhoven", "NL"), ("Rotterdam", "NL"),
    ("Brussels", "BE", "bruxelles", "brussel"), ("Leuven", "BE"),
    ("Zurich", "CH", "zürich"), ("Geneva", "CH", "genève", "genf"),
    ("Lausanne", "CH"), ("Basel", "CH"), ("Vienna", "AT", "wien"),
    ("Madrid", "ES"), ("Barcelona", "ES"), ("Lisbon", "PT", "lisboa"),
    ("Porto", "PT"), ("Rome", "IT", "roma"), ("Milan", "IT", "milano"),
    ("Turin", "IT", "torino"), ("Stockholm", "SE"), ("Gothenburg", "SE", "göteborg"),
    ("Copenhagen", "DK", "københavn"), ("Oslo", "NO"), ("Helsinki", "FI"),
    ("Warsaw", "PL", "warszawa"), ("Kraków", "PL", "krakow", "cracow"),
    ("Prague", "CZ", "praha"), ("Budapest", "HU"), ("Bucharest", "RO", "bucurești"),
    ("Athens", "GR", "αθήνα"), ("Kyiv", "UA", "kiev", "київ"), ("Tallinn", "EE"),
    ("Moscow", "RU", "москва"), ("Saint Petersburg", "RU", "st petersburg"),
    ("Istanbul", "TR"), ("Ankara", "TR"),
    # Middle East, Africa
    ("Dubai", "AE"), ("Abu Dhabi", "AE"), ("Riyadh", "SA"), ("Doha", "QA"),
    ("Tel Aviv", "IL"), ("Haifa", "IL"), ("Cairo", "EG"), ("Lagos", "NG"),
    ("Nairobi", "KE"), ("Cape Town", "ZA"), ("Johannesburg", "ZA"),
    ("Accra", "GH"), ("Kigali", None), ("Casablanca", "MA"),
    # Asia Pacific
    ("Singapore", "SG"), ("Beijing", "CN", "北京", "peking"),
    ("Shanghai", "CN", "上海"), ("Shenzhen", "CN", "深圳"), ("Hangzhou", "CN", "杭州"),
    ("Guangzhou", "CN", "广州"), ("Chengdu", "CN"), ("Nanjing", "CN"), ("Wuhan", "CN"),
    ("Tokyo", "JP", "東京"), ("Osaka", "JP", "大阪"), ("Kyoto", "JP", "京都"),
    ("Seoul", "KR", "서울"), ("Daejeon", "KR"), ("Taipei", "TW", "台北", "臺北"),
    ("Bangkok", "TH"), ("Jakarta", "ID"), ("Kuala Lumpur", "MY"),
    ("Manila", "PH"), ("Ho Chi Minh City", "VN", "saigon"), ("Hanoi", "VN"),
    ("Karachi", "PK"), ("Lahore", "PK"), ("Islamabad", "PK"), ("Dhaka", "BD"),
    ("Kathmandu", "NP"),
    ("Sydney", "AU"), ("Melbourne", "AU"), ("Brisbane", "AU"), ("Canberra", "AU"),
    ("Auckland", "NZ"),
)

COUNTRIES: dict[str, str] = {}
for _iso, _names in _COUNTRY_NAMES.items():
    for _n in _names:
        COUNTRIES[fold(_n)] = _iso

DEMONYMS: dict[str, str] = {}
for _iso, _names in _DEMONYMS.items():
    for _n in _names:
        DEMONYMS[fold(_n)] = _iso

# folded spelling -> display name
PLACES: dict[str, str] = {}
# folded spelling -> every folded spelling of the same place
PLACE_SYNONYMS: dict[str, tuple[str, ...]] = {}
# folded spelling -> ISO country, only where the city is unambiguous
CITY_COUNTRY: dict[str, str] = {}
for _display, _iso, *_alts in _CITIES:
    _spellings = tuple(dict.fromkeys(fold(s) for s in (_display, *_alts)))
    for _s in _spellings:
        PLACES.setdefault(_s, _display)
        if len(_spellings) > 1:
            PLACE_SYNONYMS[_s] = _spellings
        if _iso:
            CITY_COUNTRY[_s] = _iso


def _parts(text: str | None) -> list[str]:
    """Comma/slash-separated parts of a location, folded, last part first."""
    import re

    from .textnorm import words

    return [" ".join(words(p)) for p in reversed(re.split(r"[,/|;]| - |\(|\)", text or ""))
            if words(p)]


def country_in_text(text: str | None) -> str | None:
    """The ISO country a free-text location states outright, if any.

    A whole part must name the country ("Bangalore, India", "Zürich,
    Schweiz"). A demonym buried inside a longer part does not count:
    "Indian Institute of Science" and "American Express" are not places.
    """
    for part in _parts(text):
        if part in COUNTRIES:
            return COUNTRIES[part]
        if part in DEMONYMS:
            return DEMONYMS[part]
    return None


def countries_named(text: str | None) -> set[str]:
    """Every country TEXT places itself in: a part that IS a country ("IBM
    (India)", "Govt. of NCT of Delhi"), or an unambiguous major city anywhere
    in it ("Indian Institute of Technology Kanpur"). All of them, not the
    first: "New York University Abu Dhabi" names two, and is in neither for
    certain."""
    found = set()
    for part in _parts(text):
        if part in COUNTRIES:
            found.add(COUNTRIES[part])
        ws = part.split()
        for n in (3, 2, 1):
            for i in range(len(ws) - n + 1):
                gram = " ".join(ws[i:i + n])
                if gram in CITY_COUNTRY:
                    found.add(CITY_COUNTRY[gram])
    return found


def country_of_employers(current: list[str], past: list[str]) -> str | None:
    """The country someone's workplaces put them in, or None if they disagree.

    For people no source places and whose location says nothing. Workplaces
    only -- where someone studied is not where they are. With a current
    workplace, only current ones count, even when none can be placed: someone
    now at Lund University was put in Britain by a past job at the LSE,
    because Lund is not in the gazetteer. With none, every past workplace
    must name the same single country. A name that places itself in two
    countries counts for neither.
    """
    named: set[str] = set()
    for name in current or past:
        places = countries_named(name)
        if len(places) == 1:
            named |= places
    return named.pop() if len(named) == 1 else None


def city_country(text: str | None) -> str | None:
    """The country of an unambiguous major city named in TEXT, if any."""
    for part in _parts(text):
        ws = part.split()
        for n in (3, 2, 1):
            for i in range(len(ws) - n + 1):
                gram = " ".join(ws[i:i + n])
                if gram in CITY_COUNTRY:
                    return CITY_COUNTRY[gram]
    return None
