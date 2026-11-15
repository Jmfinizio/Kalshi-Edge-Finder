"""Team data per sport.  Row = (canonical abbr, city, nickname, abbreviation aliases, extra full-name aliases).

Canonical abbreviations follow the league's common usage; the alias lists absorb the variants Kalshi and sportsbooks use.
If Kalshi's real tickers use a code that is missing here, the market shows up under Diagnostics -> unmatched: add the code
as an alias and restart.
"""

NFL = [
    ("ARI", "Arizona", "Cardinals", ("AZ",), ()), ("ATL", "Atlanta", "Falcons", (), ()), ("BAL", "Baltimore", "Ravens", (), ()),
    ("BUF", "Buffalo", "Bills", (), ()), ("CAR", "Carolina", "Panthers", (), ()), ("CHI", "Chicago", "Bears", (), ()),
    ("CIN", "Cincinnati", "Bengals", (), ()), ("CLE", "Cleveland", "Browns", (), ()), ("DAL", "Dallas", "Cowboys", (), ()),
    ("DEN", "Denver", "Broncos", (), ()), ("DET", "Detroit", "Lions", (), ()), ("GB", "Green Bay", "Packers", ("GNB",), ()),
    ("HOU", "Houston", "Texans", (), ()), ("IND", "Indianapolis", "Colts", (), ()), ("JAX", "Jacksonville", "Jaguars", ("JAC",), ()),
    ("KC", "Kansas City", "Chiefs", ("KAN",), ()), ("LV", "Las Vegas", "Raiders", ("LVR", "OAK"), ()),
    ("LAC", "Los Angeles", "Chargers", ("SD",), ()), ("LA", "Los Angeles", "Rams", ("LAR", "STL"), ()),
    ("MIA", "Miami", "Dolphins", (), ()), ("MIN", "Minnesota", "Vikings", (), ()), ("NE", "New England", "Patriots", ("NWE",), ()),
    ("NO", "New Orleans", "Saints", ("NOR",), ()), ("NYG", "New York", "Giants", (), ()), ("NYJ", "New York", "Jets", (), ()),
    ("PHI", "Philadelphia", "Eagles", (), ()), ("PIT", "Pittsburgh", "Steelers", (), ()), ("SF", "San Francisco", "49ers", ("SFO",), ()),
    ("SEA", "Seattle", "Seahawks", (), ()), ("TB", "Tampa Bay", "Buccaneers", ("TAM",), ()), ("TEN", "Tennessee", "Titans", (), ()),
    ("WAS", "Washington", "Commanders", ("WSH",), ()),
]

NBA = [
    ("ATL", "Atlanta", "Hawks", (), ()), ("BOS", "Boston", "Celtics", (), ()), ("BKN", "Brooklyn", "Nets", ("BRK",), ()),
    ("CHA", "Charlotte", "Hornets", ("CHO",), ()), ("CHI", "Chicago", "Bulls", (), ()), ("CLE", "Cleveland", "Cavaliers", (), ()),
    ("DAL", "Dallas", "Mavericks", (), ()), ("DEN", "Denver", "Nuggets", (), ()), ("DET", "Detroit", "Pistons", (), ()),
    ("GSW", "Golden State", "Warriors", ("GS",), ()), ("HOU", "Houston", "Rockets", (), ()), ("IND", "Indiana", "Pacers", (), ()),
    ("LAC", "Los Angeles", "Clippers", (), ("LA Clippers",)), ("LAL", "Los Angeles", "Lakers", (), ("LA Lakers",)),
    ("MEM", "Memphis", "Grizzlies", (), ()), ("MIA", "Miami", "Heat", (), ()), ("MIL", "Milwaukee", "Bucks", (), ()),
    ("MIN", "Minnesota", "Timberwolves", (), ()), ("NOP", "New Orleans", "Pelicans", ("NO", "NOH"), ()),
    ("NYK", "New York", "Knicks", ("NY",), ()), ("OKC", "Oklahoma City", "Thunder", (), ()), ("ORL", "Orlando", "Magic", (), ()),
    ("PHI", "Philadelphia", "76ers", ("PHL",), ()), ("PHX", "Phoenix", "Suns", ("PHO",), ()),
    ("POR", "Portland", "Trail Blazers", (), ("Portland Blazers",)), ("SAC", "Sacramento", "Kings", (), ()),
    ("SAS", "San Antonio", "Spurs", ("SA",), ()), ("TOR", "Toronto", "Raptors", (), ()), ("UTA", "Utah", "Jazz", (), ()),
    ("WAS", "Washington", "Wizards", ("WSH",), ()),
]

WNBA = [
    ("ATL", "Atlanta", "Dream", (), ()), ("CHI", "Chicago", "Sky", (), ()), ("CON", "Connecticut", "Sun", ("CT",), ()),
    ("DAL", "Dallas", "Wings", (), ()), ("GSV", "Golden State", "Valkyries", ("GS",), ()), ("IND", "Indiana", "Fever", (), ()),
    ("LVA", "Las Vegas", "Aces", ("LV",), ()), ("LAS", "Los Angeles", "Sparks", ("LA",), ()), ("MIN", "Minnesota", "Lynx", (), ()),
    ("NYL", "New York", "Liberty", ("NY",), ()), ("PHX", "Phoenix", "Mercury", ("PHO",), ()), ("POR", "Portland", "Fire", (), ()),
    ("SEA", "Seattle", "Storm", (), ()), ("TOR", "Toronto", "Tempo", (), ()), ("WAS", "Washington", "Mystics", ("WSH",), ()),
]

NHL = [
    ("ANA", "Anaheim", "Ducks", (), ()), ("BOS", "Boston", "Bruins", (), ()), ("BUF", "Buffalo", "Sabres", (), ()),
    ("CGY", "Calgary", "Flames", ("CAL",), ()), ("CAR", "Carolina", "Hurricanes", (), ()), ("CHI", "Chicago", "Blackhawks", (), ()),
    ("COL", "Colorado", "Avalanche", (), ()), ("CBJ", "Columbus", "Blue Jackets", ("CLB",), ()), ("DAL", "Dallas", "Stars", (), ()),
    ("DET", "Detroit", "Red Wings", (), ()), ("EDM", "Edmonton", "Oilers", (), ()), ("FLA", "Florida", "Panthers", (), ()),
    ("LAK", "Los Angeles", "Kings", ("LA",), ()), ("MIN", "Minnesota", "Wild", (), ()), ("MTL", "Montreal", "Canadiens", ("MON",), ()),
    ("NSH", "Nashville", "Predators", ("NAS",), ()), ("NJD", "New Jersey", "Devils", ("NJ",), ()), ("NYI", "New York", "Islanders", (), ()),
    ("NYR", "New York", "Rangers", (), ()), ("OTT", "Ottawa", "Senators", (), ()), ("PHI", "Philadelphia", "Flyers", (), ()),
    ("PIT", "Pittsburgh", "Penguins", (), ()), ("SEA", "Seattle", "Kraken", (), ()), ("SJS", "San Jose", "Sharks", ("SJ",), ()),
    ("STL", "St. Louis", "Blues", (), ()), ("TBL", "Tampa Bay", "Lightning", ("TB", "TAM"), ()), ("TOR", "Toronto", "Maple Leafs", (), ()),
    ("UTA", "Utah", "Mammoth", ("UTAH",), ("Utah Hockey Club",)), ("VAN", "Vancouver", "Canucks", (), ()),
    ("VGK", "Vegas", "Golden Knights", ("VEG", "LVG"), ("Las Vegas Golden Knights",)), ("WSH", "Washington", "Capitals", ("WAS",), ()),
    ("WPG", "Winnipeg", "Jets", (), ()),
]

MLB = [
    ("ARI", "Arizona", "Diamondbacks", ("AZ",), ()), ("ATH", "Oakland", "Athletics", ("OAK",), ("Athletics", "Sacramento Athletics")),
    ("ATL", "Atlanta", "Braves", (), ()), ("BAL", "Baltimore", "Orioles", (), ()), ("BOS", "Boston", "Red Sox", (), ()),
    ("CHC", "Chicago", "Cubs", (), ()), ("CWS", "Chicago", "White Sox", ("CHW", "CHA"), ("Chicago WS",)), ("CIN", "Cincinnati", "Reds", (), ()),
    ("CLE", "Cleveland", "Guardians", (), ()), ("COL", "Colorado", "Rockies", (), ()), ("DET", "Detroit", "Tigers", (), ()),
    ("HOU", "Houston", "Astros", (), ()), ("KC", "Kansas City", "Royals", ("KCR",), ()), ("LAA", "Los Angeles", "Angels", ("ANA",), ("LA Angels",)),
    ("LAD", "Los Angeles", "Dodgers", (), ("LA Dodgers",)), ("MIA", "Miami", "Marlins", ("FLA",), ()), ("MIL", "Milwaukee", "Brewers", (), ()),
    ("MIN", "Minnesota", "Twins", (), ()), ("NYM", "New York", "Mets", (), ()), ("NYY", "New York", "Yankees", (), ()),
    ("PHI", "Philadelphia", "Phillies", (), ()), ("PIT", "Pittsburgh", "Pirates", (), ()), ("SD", "San Diego", "Padres", ("SDP",), ()),
    ("SF", "San Francisco", "Giants", ("SFG",), ()), ("SEA", "Seattle", "Mariners", (), ()), ("STL", "St. Louis", "Cardinals", (), ()),
    ("TB", "Tampa Bay", "Rays", ("TBR",), ()), ("TEX", "Texas", "Rangers", (), ()), ("TOR", "Toronto", "Blue Jays", (), ()),
    ("WSH", "Washington", "Nationals", ("WAS",), ()),
]

TABLES = {"nfl": NFL, "nba": NBA, "wnba": WNBA, "nhl": NHL, "mlb": MLB}
EXPECTED_COUNTS = {"nfl": 32, "nba": 30, "wnba": 15, "nhl": 32, "mlb": 30}
