"""Indian states and aliases accepted by Tally."""

TALLY_STATES = (
    "Andaman & Nicobar Islands",
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chandigarh",
    "Chhattisgarh",
    "Dadra & Nagar Haveli and Daman & Diu",
    "Delhi",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jammu & Kashmir",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Ladakh",
    "Lakshadweep",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Puducherry",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
)

ALIASES = {
    "jammu and kashmir": "Jammu & Kashmir",
    "orissa": "Odisha",
    "pondicherry": "Puducherry",
    "uttaranchal": "Uttarakhand",
    "andaman and nicobar islands": "Andaman & Nicobar Islands",
    "chattisgarh": "Chhattisgarh",
    "chhatisgarh": "Chhattisgarh",
    "dadra and nagar haveli": "Dadra & Nagar Haveli and Daman & Diu",
    "daman and diu": "Dadra & Nagar Haveli and Daman & Diu",
    "new delhi": "Delhi",
    "nct of delhi": "Delhi",
    "an": "Andaman & Nicobar Islands",
    "ap": "Andhra Pradesh",
    "ar": "Arunachal Pradesh",
    "as": "Assam",
    "br": "Bihar",
    "ch": "Chandigarh",
    "ct": "Chhattisgarh",
    "cg": "Chhattisgarh",
    "dn": "Dadra & Nagar Haveli and Daman & Diu",
    "dd": "Dadra & Nagar Haveli and Daman & Diu",
    "dl": "Delhi",
    "ga": "Goa",
    "gj": "Gujarat",
    "hr": "Haryana",
    "hp": "Himachal Pradesh",
    "jk": "Jammu & Kashmir",
    "jh": "Jharkhand",
    "ka": "Karnataka",
    "kl": "Kerala",
    "la": "Ladakh",
    "ld": "Lakshadweep",
    "mp": "Madhya Pradesh",
    "mh": "Maharashtra",
    "mn": "Manipur",
    "ml": "Meghalaya",
    "mz": "Mizoram",
    "nl": "Nagaland",
    "or": "Odisha",
    "od": "Odisha",
    "py": "Puducherry",
    "pb": "Punjab",
    "rj": "Rajasthan",
    "sk": "Sikkim",
    "tn": "Tamil Nadu",
    "tg": "Telangana",
    "ts": "Telangana",
    "tr": "Tripura",
    "up": "Uttar Pradesh",
    "ut": "Uttarakhand",
    "uk": "Uttarakhand",
    "wb": "West Bengal",
    "the dadra and nagar haveli and daman and diu": "Dadra & Nagar Haveli and Daman & Diu",
    "dadra and nagar haveli and daman and diu": "Dadra & Nagar Haveli and Daman & Diu",
}


def _key(text):
    return " ".join(str(text or "").casefold().replace("&", "and").split())


STATE_LOOKUP = {_key(state): state for state in TALLY_STATES}
STATE_LOOKUP.update({_key(alias): state for alias, state in ALIASES.items()})


def canonical_state(text):
    return STATE_LOOKUP.get(_key(text))


def require_india_state(state, country):
    country_key = " ".join(str(country or "").casefold().split())
    if country_key not in ("", "india", "in", "bharat"):
        return state
    canonical = canonical_state(state)
    if canonical is None:
        raise ValueError("Please choose your state from the list")
    return canonical


def state_for_site(state, country, is_india_site):
    return require_india_state(state, country) if is_india_site else state


def template_context(prefill_state=None):
    return {
        "india_states": TALLY_STATES,
        "india_state_lookup": STATE_LOOKUP,
        "prefill_state": canonical_state(prefill_state) or "",
    }
