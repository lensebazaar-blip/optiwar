#!/usr/bin/env python3
"""Language, script and intent of one customer message — deterministic, no I/O.

The conversation model does the semantic understanding and the reply. This
module is the part that must not depend on a model: it states which language
and script a customer is writing in (so the reply can follow it), which
canonical intent family a message belongs to (so an authoritative read such as
LOOKUP_PRESCRIPTION is offered to the model before it answers), and the
escalation-reason taxonomy a ticket is filed under. Every value it returns is a
code, never customer text, so it can be written to ``ai_events`` as-is.

The 22 Eighth-Schedule languages are identified by script (and, inside a
shared script, by marker words); Hindi written in Roman letters (Hinglish) by
a small function-word lexicon folded through a phonetic skeleton, so the
spelling a phone keyboard or voice-to-text produces ("chashme k number",
"chasma", "nambar", "bnega") still lands on the same word.
"""
import re
import unicodedata

# ─── canonical codes ───

INTENT_PRESCRIPTION_CONFIRMATION = "PRESCRIPTION_CONFIRMATION"
INTENT_PRESCRIPTION_STATUS = "PRESCRIPTION_STATUS"
INTENT_PRESCRIPTION_FOR_ORDER = "PRESCRIPTION_FOR_ORDER"
INTENT_PRESCRIPTION_ENTRY_HELP = "PRESCRIPTION_ENTRY_HELP"
INTENT_ORDER_STATUS = "ORDER_STATUS"
INTENT_RESHIP_STATUS = "RESHIP_STATUS"
INTENT_PAYMENT_STATUS = "PAYMENT_STATUS"
INTENT_HUMAN_REQUEST = "HUMAN_REQUEST"
INTENT_CALLBACK_REQUEST = "CALLBACK_REQUEST"
INTENT_PRODUCT_SEARCH = "PRODUCT_SEARCH"
INTENT_GREETING = "GREETING"
INTENT_OTHER = "OTHER"

PRESCRIPTION_INTENTS = (INTENT_PRESCRIPTION_CONFIRMATION, INTENT_PRESCRIPTION_STATUS,
                        INTENT_PRESCRIPTION_FOR_ORDER, INTENT_PRESCRIPTION_ENTRY_HELP)
INTENTS = PRESCRIPTION_INTENTS + (
    INTENT_ORDER_STATUS, INTENT_RESHIP_STATUS, INTENT_PAYMENT_STATUS, INTENT_HUMAN_REQUEST,
    INTENT_CALLBACK_REQUEST, INTENT_PRODUCT_SEARCH, INTENT_GREETING, INTENT_OTHER)

# The deterministic capability an intent is served by. Language never changes
# this mapping: "mera saved number dikhao" and "show my saved prescription" are
# the same LOOKUP_PRESCRIPTION.
INTENT_CAPABILITY = {
    INTENT_PRESCRIPTION_CONFIRMATION: "LOOKUP_PRESCRIPTION",
    INTENT_PRESCRIPTION_STATUS: "LOOKUP_PRESCRIPTION",
    INTENT_PRESCRIPTION_FOR_ORDER: "LOOKUP_PRESCRIPTION",
    INTENT_PRESCRIPTION_ENTRY_HELP: "NAVIGATE",
    INTENT_ORDER_STATUS: "LOOKUP_ORDER",
    INTENT_RESHIP_STATUS: "LOOKUP_RESHIP_STATUS",
    INTENT_PAYMENT_STATUS: "LOOKUP_ORDER",
    INTENT_HUMAN_REQUEST: "CREATE_TICKET",
    INTENT_CALLBACK_REQUEST: "CREATE_TICKET",
    INTENT_PRODUCT_SEARCH: "NAVIGATE",
}

# Intents the assistant can answer itself; a ticket filed for one of these
# without an answer is an AI failure, not a customer choice.
ANSWERABLE_INTENTS = PRESCRIPTION_INTENTS + (
    INTENT_ORDER_STATUS, INTENT_RESHIP_STATUS, INTENT_PAYMENT_STATUS, INTENT_PRODUCT_SEARCH)

# Intents answered from the customer's own account: a signed-out customer is
# told to sign in, never asked for an order number or sent to a ticket.
ACCOUNT_INTENTS = (INTENT_PRESCRIPTION_STATUS, INTENT_PRESCRIPTION_FOR_ORDER,
                   INTENT_ORDER_STATUS, INTENT_RESHIP_STATUS, INTENT_PAYMENT_STATUS)
ORDER_LOOKUP_INTENTS = (INTENT_ORDER_STATUS, INTENT_PAYMENT_STATUS)

ESC_CUSTOMER_REQUESTED_HUMAN = "CUSTOMER_REQUESTED_HUMAN"
ESC_LANGUAGE_UNDERSTANDING_FAILED = "LANGUAGE_UNDERSTANDING_FAILED"
ESC_TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
ESC_DATA_MISSING = "DATA_MISSING"
ESC_POLICY_REQUIRES_HUMAN = "POLICY_REQUIRES_HUMAN"
ESC_AI_LOW_CONFIDENCE = "AI_LOW_CONFIDENCE"
ESC_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
ESC_UNCLASSIFIED = "UNCLASSIFIED"
ESCALATION_REASONS = (ESC_CUSTOMER_REQUESTED_HUMAN, ESC_LANGUAGE_UNDERSTANDING_FAILED,
                      ESC_TOOL_UNAVAILABLE, ESC_DATA_MISSING, ESC_POLICY_REQUIRES_HUMAN,
                      ESC_AI_LOW_CONFIDENCE, ESC_MODEL_UNAVAILABLE, ESC_UNCLASSIFIED)

FAIL_NLU_LANGUAGE = "NLU_LANGUAGE_FAILURE"
FAIL_NO_TOOL = "NO_TOOL_FOR_INTENT"
FAIL_LOW_CONFIDENCE = "LOW_CONFIDENCE"
FAIL_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"

LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "hi-Latn": "Hinglish (Hindi in Roman letters)",
    "as": "Assamese", "bn": "Bengali", "brx": "Bodo", "doi": "Dogri",
    "gu": "Gujarati", "kn": "Kannada", "ks": "Kashmiri", "kok": "Konkani",
    "mai": "Maithili", "ml": "Malayalam", "mni": "Manipuri", "mr": "Marathi",
    "ne": "Nepali", "or": "Odia", "pa": "Punjabi", "sa": "Sanskrit",
    "sat": "Santali", "sd": "Sindhi", "ta": "Tamil", "te": "Telugu", "ur": "Urdu",
    "pa-Latn": "Punjabi in Roman letters", "ta-Latn": "Tamil in Roman letters",
    "te-Latn": "Telugu in Roman letters", "bn-Latn": "Bengali in Roman letters",
    "mr-Latn": "Marathi in Roman letters", "gu-Latn": "Gujarati in Roman letters",
    "und": "undetermined",
}
EIGHTH_SCHEDULE = ("as", "bn", "brx", "doi", "gu", "hi", "kn", "ks", "kok", "mai",
                   "ml", "mni", "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta",
                   "te", "ur")

# ─── script ───

_SCRIPT_RANGES = (
    ("Deva", 0x0900, 0x097F), ("Deva", 0xA8E0, 0xA8FF),
    ("Beng", 0x0980, 0x09FF), ("Guru", 0x0A00, 0x0A7F), ("Gujr", 0x0A80, 0x0AFF),
    ("Orya", 0x0B00, 0x0B7F), ("Taml", 0x0B80, 0x0BFF), ("Telu", 0x0C00, 0x0C7F),
    ("Knda", 0x0C80, 0x0CFF), ("Mlym", 0x0D00, 0x0D7F),
    ("Arab", 0x0600, 0x06FF), ("Arab", 0x0750, 0x077F), ("Arab", 0xFB50, 0xFDFF),
    ("Arab", 0xFE70, 0xFEFF), ("Olck", 0x1C50, 0x1C7F),
    ("Mtei", 0xABC0, 0xABFF), ("Mtei", 0xAAE0, 0xAAFF),
)
_SCRIPT_LANG = {"Beng": "bn", "Guru": "pa", "Gujr": "gu", "Orya": "or", "Taml": "ta",
                "Telu": "te", "Knda": "kn", "Mlym": "ml", "Olck": "sat", "Mtei": "mni",
                "Arab": "ur", "Deva": "hi"}


def _script_of(ch):
    o = ord(ch)
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "Latn"
    for name, lo, hi in _SCRIPT_RANGES:
        if lo <= o <= hi:
            return name
    return None


def script_counts(text):
    counts = {}
    for ch in unicodedata.normalize("NFC", text or ""):
        s = _script_of(ch)
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


# Marker words that separate languages sharing one script. Whole-word match.
_DEVA_MARKERS = (
    ("mr", ("आहे", "आहेत", "माझा", "माझी", "माझे", "माझ्या", "केली", "कोणत्या",
            "होईल", "तयार", "चष्म्याचा", "टाकला", "काय")),
    ("ne", ("छ", "छन्", "मेरो", "गरेको", "हालेको", "कुन", "बन्छ", "बन्नेछ", "चस्मा", "हुन्छ")),
    ("mai", ("अछि", "हमर", "हम्मर", "कोना", "बनत", "देलहुँ", "केलहुँ")),
    ("kok", ("म्हजो", "म्हजें", "आसा", "किते", "जातलो", "घाल्लां", "चश्मो")),
    ("doi", ("कीता", "बनग", "केह्ड़े", "दा")),
    ("brx", ("आं", "नों", "जायो", "मोनसे", "बेसेबां", "दिनो")),
    ("sa", ("मया", "मम", "अस्ति", "किम्", "उपनेत्रम्", "उपनेत्रस्य", "भविष्यति", "कस्य")),
)
_BENG_ASSAMESE = ("ৰ", "ৱ")
_ARAB_SINDHI = ("ڄ", "ٻ", "ڀ", "ڏ", "ڳ", "ڃ", "ٿ", "ٽ", "ڦ", "ڪ", "ڻ")
_ARAB_KASHMIRI = ("ۄ", "ؠ", "ٲ", "ٚ", "ۆ")

# Roman-letter Hindi function words and everyday verbs, written as skeletons.
_HINGLISH_WORDS = (
    "hai hain ho hoga hogi hoge honge tha thi the kya kyu kyun kyon kaise kaisa kaisi kese "
    "kab kahan kaha kidhar kitna kitne kitni mera meri mere mujhe mujhko humko hamara "
    "hamari aap aapka aapki aapke apna apni apne tum tumhara tera teri ka ki ke ko se "
    "mein nahi nahin nhi nai haan haa ji bhi kar karo karna karne karke kiya kiye kia "
    "diya diye dia dala dali daala daal dal dalna daalna gaya gayi gya raha rahi rahe "
    "wala wali wale abhi jaldi chahiye chaiye chahie batao bataye bataiye btao dikhao "
    "dikha dekho dekhna bhejo bhej chashma chashme chasma chasme chashmah ainak aankh "
    "ankh aankhon nazar najar banega banegi banenge bnega bankar banke bana banao "
    "ayega aayega aega aaega aayegi milega milegi kis kisi kaun kon konsa kaunsa yeh "
    "woh wo lekin aur accha acha achha theek thik thek paisa paise rupaye wapas "
    "pehle pahle already bataya samajh samjha samjho bol bolo boliye madad sahi galat "
    "number wala liye lie liya sakta sakti sakte hua hui hue dalu daalu dalun daalun "
    "bharu bharun karu karun dikhaye bhejun").split()

_ROMAN_OTHER = {
    "pa-Latn": "tuhada tuhanu tusi kiddan kithe kinna sanu menu mainu ohna hunda "
               "karna ae ne vich nal gal".split(),
    "ta-Latn": "enna epdi enga eppo venum irukku illa naan unga enakku romba "
               "pannunga sollunga".split(),
    "te-Latn": "enti ela ekkada eppudu kavali undi ledu nenu meeru naaku chala "
               "cheyandi cheppandi".split(),
    "bn-Latn": "ami tumi apni kothay kokhon kemon achhe ache nei amar tomar "
               "korbo korun bolun".split(),
    "mr-Latn": "majha majhi mazha mazhi kasa kasa kay ahe aahe nahi tumcha "
               "kara sanga".split(),
    "gu-Latn": "maru mari tamaru shu kem kyare chhe che nathi karo kaho".split(),
}

_EN_WORDS = (
    "i my me mine we our you your the a an is are was were be been will would can "
    "could should have has had do does did what which when where who how why "
    "please thanks thank hello hi hey ok okay yes no not want need help added add "
    "saved save entered enter uploaded upload show tell check made make glasses "
    "spectacles spectacle specs eyeglasses lens lenses frame frames prescription "
    "power eye eyes order ordered delivery status track call callback back human "
    "agent person support ticket already this that it in on for with of to and or "
    "but if my number price cost return refund").split()

_GREETINGS = set(
    "hi hii hiii hello helo hey hlo namaste namaskar ok okay k thanks thank thx ty "
    "yes no haan han ji nahi good morning evening".split())


def skeleton(word):
    """A phonetic fold of a Roman-letter word: the spellings a keyboard,
    voice-to-text or a regional habit produce for one Hindi word collapse to
    one key ("chashme", "chasme", "chashmay" -> "casme")."""
    w = (word or "").lower()
    w = re.sub(r"[^a-z]", "", w)
    if not w:
        return ""
    for a, b in (("ee", "i"), ("oo", "u"), ("aa", "a"), ("ph", "f"), ("sh", "s"),
                 ("ch", "c"), ("th", "t"), ("dh", "d"), ("kh", "k"), ("gh", "g"),
                 ("bh", "b"), ("jh", "j"), ("w", "v"), ("z", "j"), ("q", "k"),
                 ("ay", "e"), ("ai", "e")):
        w = w.replace(a, b)
    w = re.sub(r"(.)\1+", r"\1", w)
    if len(w) > 2 and w.endswith("h"):
        w = w[:-1]
    if len(w) > 3 and w.endswith("n") and w[-2] in "aeiou":
        w = w[:-1]
    return w


def _skel_set(words):
    return {skeleton(w) for w in words if skeleton(w)}


_HINGLISH_SKEL = _skel_set(_HINGLISH_WORDS)
_EN_SET = set(_EN_WORDS)
_ROMAN_OTHER_SKEL = {k: _skel_set(v) for k, v in _ROMAN_OTHER.items()}
# English words whose skeleton collides with a Hindi one are English when the
# word is spelled the English way.
_EN_ONLY_SPELLINGS = {"the", "to", "me", "he", "she", "is", "in", "no", "so", "do",
                      "go", "a", "i", "be", "hi", "ha", "ho", "add", "did"}

# Letters plus the combining vowel signs and viramas of the Indic, Arabic,
# Meetei and Ol Chiki blocks (not letters to ``\w``), so "आं" is one word.
_TOKEN_RE = re.compile(
    r"(?:[^\W\d_]|[\u0900-\u0963\u0971-\u0D7F\u064B-\u065F\u0670\u06D6-\u06ED"
    r"\uA8E0-\uA8FF\uABC0-\uABEF\u1C50-\u1C7F\u200C\u200D])+", re.UNICODE)


def tokens(text):
    return _TOKEN_RE.findall(unicodedata.normalize("NFC", text or ""))


def _has_word(text, words):
    toks = set(tokens(text))
    return any(w in toks for w in words)


def detect(text):
    """``{language, script, code_mixed, confidence}`` for one message.

    ``language`` is a BCP-47-style code ("hi-Latn" is Hinglish); ``script`` an
    ISO 15924 code; ``confidence`` 0..1. An empty or symbol-only message is
    "und" at confidence 0."""
    counts = script_counts(text)
    if not counts:
        return {"language": "und", "script": None, "code_mixed": False,
                "confidence": 0.0}
    latin = counts.get("Latn", 0)
    native = {k: v for k, v in counts.items() if k != "Latn"}
    toks = tokens(text)
    latin_toks = [t.lower() for t in toks if _script_of(t[0]) == "Latn"]
    en_hits = sum(1 for t in latin_toks if t in _EN_SET)

    if native:
        script = max(native, key=native.get)
        lang = _SCRIPT_LANG.get(script, "und")
        conf = 0.95
        if script == "Deva":
            for code, markers in _DEVA_MARKERS:
                if _has_word(text, markers):
                    lang, conf = code, 0.8
                    break
            else:
                conf = 0.85  # Hindi, unless a sibling's marker word says otherwise
        elif script == "Beng" and any(c in text for c in _BENG_ASSAMESE):
            lang = "as"
        elif script == "Arab":
            if any(c in text for c in _ARAB_KASHMIRI):
                lang, conf = "ks", 0.8
            elif any(c in text for c in _ARAB_SINDHI):
                lang, conf = "sd", 0.85
            else:
                conf = 0.85
        code_mixed = latin > 0 and bool(latin_toks)
        return {"language": lang, "script": script, "code_mixed": code_mixed,
                "confidence": conf}

    # Roman letters only: English, Hinglish, or another language romanised.
    if not latin_toks:
        return {"language": "und", "script": "Latn", "code_mixed": False,
                "confidence": 0.0}
    hi_hits = 0
    for t in latin_toks:
        if t in _EN_ONLY_SPELLINGS:
            continue
        if skeleton(t) in _HINGLISH_SKEL and not (t in _EN_SET and t not in ("number",)):
            hi_hits += 1
    other = {code: sum(1 for t in latin_toks if skeleton(t) in sk and t not in _EN_SET)
             for code, sk in _ROMAN_OTHER_SKEL.items()}
    best_other = max(other, key=other.get)
    n = len(latin_toks)
    if other[best_other] >= 2 and other[best_other] > hi_hits:
        return {"language": best_other, "script": "Latn", "code_mixed": en_hits > 0,
                "confidence": round(min(0.9, 0.4 + 0.15 * other[best_other]), 2)}
    if hi_hits >= 2 or (hi_hits == 1 and n <= 3 and en_hits == 0):
        conf = min(0.95, 0.45 + 0.1 * hi_hits + 0.3 * hi_hits / max(n, 1))
        return {"language": "hi-Latn", "script": "Latn", "code_mixed": en_hits > 0,
                "confidence": round(conf, 2)}
    conf = 0.9 if en_hits >= max(1, n // 2) else 0.6
    return {"language": "en", "script": "Latn", "code_mixed": hi_hits > 0,
            "confidence": conf}


def is_meaningful(text):
    """A message that carries a language choice: not a bare greeting, "ok" or
    one-word loan ("Callback", "yes"). A native-script message always does."""
    counts = script_counts(text)
    if any(k != "Latn" for k in counts):
        return True
    toks = [t.lower() for t in tokens(text)]
    lexical = [t for t in toks if t not in _GREETINGS]
    return len(lexical) >= 3


def conversation_language(user_messages):
    """The language to reply in: the most recent *meaningful* customer message
    wins (so a customer who switches language is followed), a greeting or a
    one-word reply does not switch it. Falls back to the latest message."""
    msgs = [m for m in (user_messages or []) if m and m.strip()]
    for m in reversed(msgs):
        if is_meaningful(m):
            return detect(m)
    if msgs:
        return detect(msgs[-1])
    return detect("")


def bucket(language):
    """Report bucket: English / Hindi / Hinglish / Other Indian / Unknown."""
    if language == "en":
        return "English"
    if language == "hi":
        return "Hindi"
    if language == "hi-Latn":
        return "Hinglish"
    if language in (None, "", "und"):
        return "Unknown"
    return "Other Indian"


# ─── intent ───

def _concept(latin, native):
    return (_skel_set(latin), tuple(native))


_C_RX = _concept(
    "prescription prescriptions presciption prescripton priscription perscription "
    "prescrption rx sph cyl axis".split(),
    ("प्रिस्क्रिप्शन", "पर्चा", "नुस्खा", "प्रेस्क्रिप्शन", "প্রেসক্রিপশন", "ப்ரிஸ்கிரிப்ஷன்",
     "ప్రిస్క్రిప్షన్", "ಪ್ರಿಸ್ಕ್ರಿಪ್ಷನ್", "പ്രിസ്ക്രിപ്ഷൻ", "ਪ੍ਰਿਸਕ੍ਰਿਪਸ਼ਨ", "પ્રિસ્ક્રિપ્શન",
     "ପ୍ରେସକ୍ରିପସନ", "نسخہ", "نسخو"))
_C_SPECS = _concept(
    "chashma chashme chasma chasme chashmah chashmay chasmah specs spec spectacles "
    "spectacle glasses glass eyeglasses eyeglass ainak ainaak enak lens lenses "
    "kannadi kannadam kalladalu chosma chosmo".split(),
    ("चश्म", "चष्म", "चस्म", "चसम", "ऐनक", "उपनेत्र", "ચશ્મ", "ਚਸ਼ਮ", "ਐਨਕ", "চশম", "চছম",
     "ଚଷମ", "கண்ணாடி", "కళ్లద్దా", "కళ్ళద్దా", "కళ్ళజోడు", "ಕನ್ನಡಕ", "കണ്ണട", "چشم",
     "عینک", "عينڪ", "ᱪᱚᱥᱢᱟ", "ꯃꯤꯠꯄꯥꯜ"))
_C_EYE = _concept(
    "aankh ankh aankhon ankhon ankho eye eyes nazar najar netra".split(),
    ("आंख", "आँख", "नजर", "नज़र", "नेत्र", "डोळ", "আঁখি", "চোখ", "চকু", "கண்", "కంటి",
     "కన్ను", "ಕಣ್ಣ", "കണ്ണ്", "ਅੱਖ", "આંખ", "ଆଖି", "آنکھ", "اکھ", "ᱢᱮᱫ"))
_C_POWER = _concept(
    "power pawar powar pwr".split(),
    ("पावर", "পাওয়ার", "পাৱাৰ", "પાવર", "ਪਾਵਰ", "ପାୱାର", "பவர்", "పవర్", "ಪವರ್",
     "പവർ", "پاور"))
_C_NUMBER = _concept(
    "number numbr nmbr nambar numbar nambr num power pawar powar pwr".split(),
    ("नंबर", "नम्बर", "पावर", "संख्या", "सङ्ख्या", "নম্বর", "নম্বৰ", "পাওয়ার", "পাৱাৰ",
     "નંબર", "પાવર", "ਨੰਬਰ", "ਪਾਵਰ", "ନମ୍ବର", "ପାୱାର", "நம்பர்", "பவர்", "எண்",
     "నంబర్", "పవర్", "ನಂಬರ್", "ಪವರ್", "നമ്പർ", "പവർ", "نمبر", "پاور", "ᱱᱚᱢᱵᱚᱨ",
     "ꯅꯝꯕꯔ"))
_C_PAST_ENTRY = _concept(
    "added adding kiya kia kiye diya dia diye dala dali daala dal daal daldiya "
    "saved entered uploaded filled submitted bhara bhar already pehle pahle "
    "given gave provided put".split(),
    ("डाल", "डाला", "डाली", "दिया", "किया", "सेव", "जोड़", "जोड", "भर", "दर्ज", "टाकला", "होबाय", "योजित",
     "हालेको", "घाल्ल", "देलहुँ", "केलहुँ", "দিয়েছি", "দিছো", "সেভ", "যোগ", "ਪਾ", "ਜੋੜ",
     "ਸੇਵ", "ਦਿੱਤ", "ઉમેર", "સેવ", "આપ", "ଯୋଡ", "ସେଭ", "ଦେଇ", "சேர்த்", "சேமி",
     "கொடுத்த", "జోడించ", "సేవ్", "ఇచ్చా", "ಸೇರಿಸ", "ಸೇವ್", "ಕೊಟ್ಟ", "ചേർത്ത",
     "സേവ്", "നൽകി", "شامل", "ڈال", "محفوظ", "دیا", "ڏنو", "دِیُت", "ᱮᱢ", "ꯍꯥꯞꯈ"))
_C_SAVED = _concept(
    "saved save sav sevd".split(),
    ("सेव", "सहेज"))
_C_WHICH_MADE = _concept(
    "kis kaun kon konsa kaunsa which what banega banegi banenge bnega bnenge bankar "
    "banke bana banaya banayenge made make making ayega aayega aega aaega aayegi "
    "milega hoga will prepared".split(),
    ("किस", "कौन", "बनेग", "बनेंग", "बनकर", "बनके", "बनाय", "बन", "कोणत्या", "कुन", "बन्छ",
     "बन्ने", "बनत", "कस्य", "भविष्यति", "केह्ड़", "खंयच", "जातल", "बबे", "कोन", "কোন", "কী", "কি", "বানা", "তৈরি", "হবে",
     "ਕਿਹੜ", "ਕਿਸ", "ਬਣੇ", "ਬਣ", "કયા", "કયો", "બન", "କେଉଁ", "ତିଆରି", "ହେବ", "எந்த",
     "என்ன", "செய்ய", "ఏ", "ఏమి", "తయార", "ಯಾವ", "ತಯಾರ", "ഏത്", "ഉണ്ടാക്ക", "کس",
     "کون", "کیا", "بنے", "ڪهڙ", "کَمہِ", "بنِ", "ᱪᱮᱫ", "ꯀꯔꯝ"))
_C_SHOW = _concept(
    "show dikhao dikhaiye dikha dekh dekhna batao bataye bataiye btao tell check "
    "confirm confirmed verify saved see view display".split(),
    ("दिखा", "बता", "देख", "चेक", "दाखवा", "দেখা", "বল", "ਦਿਖਾ", "ਦੱਸ", "બતાવ",
     "ଦେଖା", "காட்டு", "చూపించ", "ತೋರಿಸ", "കാണിക്ക", "دکھا", "بتا"))
_C_ORDER = _concept(
    "order orders ordr oder ordered parcel package delivery deliver dispatch "
    "dispatched shipped shipping courier track tracking".split(),
    ("ऑर्डर", "आर्डर", "ऑडर", "पार्सल", "डिलीवरी", "অর্ডার", "ਆਰਡਰ", "ઓર્ડર", "ଅର୍ଡର",
     "ஆர்டர்", "ఆర్డర్", "ಆರ್ಡರ್", "ഓർഡർ", "آرڈر"))
_C_WHEN_WHERE = _concept(
    "kab when where kahan kaha kidhar status track tracking reach arrive "
    "pahunchega pahuchega aayega ayega aega".split(),
    ("कब", "कहाँ", "कहां", "पहुंच", "आएग", "আসবে", "কখন", "ਕਦੋਂ", "ક્યારે", "କେବେ",
     "எப்போது", "ఎప్పుడు", "ಯಾವಾಗ", "എപ്പോൾ", "کب"))
_C_HOW = _concept(
    "kaise kese kaisey how where kahan kaha kidhar steps".split(),
    ("कैसे", "कहाँ", "कहां", "कसे", "কিভাবে", "ਕਿਵੇਂ", "કેવી", "କିପରି", "எப்படி",
     "ఎలా", "ಹೇಗೆ", "എങ്ങനെ", "کیسے"))
_C_ENTRY_VERB = _concept(
    "add enter upload type fill put daalu dalu daalun dalun bharu bharun karu karun "
    "submit".split(),
    ("डालूं", "डालू", "भरूं", "भरू", "अपलोड", "जोड़ूं", "जोड़ू"))
_C_HUMAN = _concept(
    "human agent person insaan insan supervisor representative executive "
    "manager staff".split(),
    ("इंसान", "एजेंट", "व्यक्ति", "मनुष्य", "मानव"))
_C_CALL = _concept(
    "callback call phone ring baat".split(),
    ("कॉल", "फोन", "फ़ोन", "बात"))
_C_RESHIP = _concept(
    "returned return wapas vapas reship reshipping reshipment rto".split(),
    ("वापस", "रिटर्न"))
_C_PAYMENT = _concept(
    "payment paymnt payement pay paid paisa paise amount refund refunded deducted "
    "debited debit kata txn transaction upi razorpay".split(),
    ("पेमेंट", "भुगतान", "पैसे", "पैसा", "रिफंड", "कट"))
_C_PRODUCT = _concept(
    "frame frames sunglasses buy price cheap round square aviator cateye "
    "rimless kids color colour".split(),
    ("फ्रेम", "खरीद", "दाम", "कीमत"))


def _norm_for_match(text):
    t = unicodedata.normalize("NFC", text or "")
    toks = tokens(t)
    latin = set()
    for w in toks:
        if _script_of(w[0]) == "Latn":
            latin.add(skeleton(w))
    return t, latin


def _near(sk, target):
    """One edit apart, for words long enough that one edit is a typo."""
    if len(target) < 6 or abs(len(sk) - len(target)) > 1:
        return False
    if len(sk) == len(target):
        return sum(1 for a, b in zip(sk, target) if a != b) <= 1
    short, long_ = (sk, target) if len(sk) < len(target) else (target, sk)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


def _hit(concept, text, latin):
    skels, native = concept
    if latin & skels:
        return True
    for sk in latin:
        for target in skels:
            if _near(sk, target):
                return True
    return any(n in text for n in native)


def classify_intent(text):
    """``(intent, confidence)`` for one customer message.

    Rules over concepts, not phrases: a spectacle/eye word with a number/power
    word is a prescription, whatever the language; what is asked about it
    (which power it will be made in, whether it was saved, how to enter it,
    which order it is for) picks the member of the family."""
    if not text or not text.strip():
        return INTENT_OTHER, 0.0
    t, latin = _norm_for_match(text)
    has = {name: _hit(c, t, latin) for name, c in (
        ("rx", _C_RX), ("power", _C_POWER), ("specs", _C_SPECS), ("eye", _C_EYE), ("number", _C_NUMBER),
        ("past", _C_PAST_ENTRY), ("saved", _C_SAVED), ("which", _C_WHICH_MADE), ("show", _C_SHOW),
        ("order", _C_ORDER), ("when", _C_WHEN_WHERE), ("how", _C_HOW),
        ("entry", _C_ENTRY_VERB), ("human", _C_HUMAN), ("call", _C_CALL),
        ("reship", _C_RESHIP), ("payment", _C_PAYMENT), ("product", _C_PRODUCT))}
    rx_context = (has["rx"] or has["power"] or (has["number"] and (has["specs"] or has["eye"]))
                  or (has["number"] and has["saved"] and has["show"]))
    if rx_context:
        if has["how"] and not has["past"] and (has["entry"] or has["rx"]):
            return INTENT_PRESCRIPTION_ENTRY_HELP, 0.8
        if has["past"] and has["which"]:
            return INTENT_PRESCRIPTION_CONFIRMATION, 0.9
        if has["order"]:
            return INTENT_PRESCRIPTION_FOR_ORDER, 0.8
        if has["which"]:
            return INTENT_PRESCRIPTION_CONFIRMATION, 0.75
        if has["show"] or has["past"]:
            return INTENT_PRESCRIPTION_STATUS, 0.75
        return INTENT_PRESCRIPTION_STATUS, 0.55
    if has["human"]:
        return INTENT_HUMAN_REQUEST, 0.85
    if has["call"] and not has["specs"]:
        return INTENT_CALLBACK_REQUEST, 0.8
    if has["reship"] and (has["order"] or has["specs"] or has["how"] or has["when"]):
        return INTENT_RESHIP_STATUS, 0.7
    if has["payment"]:
        return INTENT_PAYMENT_STATUS, 0.7
    if has["order"] or (has["specs"] and has["when"]):
        return INTENT_ORDER_STATUS, 0.7
    if has["product"] or has["specs"]:
        return INTENT_PRODUCT_SEARCH, 0.55
    toks = [w.lower() for w in tokens(text)]
    if toks and all(w in _GREETINGS for w in toks):
        return INTENT_GREETING, 0.9
    return INTENT_OTHER, 0.3


def conversation_intent(user_messages, window=4):
    """The canonical intent of the conversation so far: the latest recent
    message with a substantive intent (a bare "yes" or "Callback" answering
    the assistant's own question does not replace what the customer asked)."""
    msgs = [m for m in (user_messages or []) if m and m.strip()][-window:]
    for m in reversed(msgs):
        intent, conf = classify_intent(m)
        if intent not in (INTENT_OTHER, INTENT_GREETING):
            if intent == INTENT_CALLBACK_REQUEST and not is_meaningful(m):
                continue
            return intent, conf
    return INTENT_OTHER, 0.3


def first_substantive_intent(user_messages):
    """The intent of the first message that asked for something: what the
    customer came for, before any reply of the assistant steered them."""
    for m in user_messages or []:
        intent, conf = classify_intent(m)
        if intent not in (INTENT_OTHER, INTENT_GREETING):
            return intent, conf
    return INTENT_OTHER, 0.3


# ─── understanding of a turn, and of a ticket ───

def understand(user_messages):
    """The non-PII understanding of the latest turn, ready for ai_events."""
    msgs = [m for m in (user_messages or []) if m and m.strip()]
    lang = conversation_language(msgs)
    intent, iconf = conversation_intent(msgs)
    return {"detected_language": lang["language"], "script": lang["script"],
            "code_mixed": lang["code_mixed"],
            "language_confidence": lang["confidence"],
            "intent": intent, "intent_confidence": iconf,
            "capability": INTENT_CAPABILITY.get(intent)}


def ticket_reason(user_messages):
    """The subject a ticket is filed under: a callback or a person when the
    customer asked for one anywhere in the chat, else what they came for."""
    intents = [classify_intent(m)[0] for m in user_messages or ()
               if m and (is_meaningful(m) or len(tokens(m)) >= 2)]
    for wanted in (INTENT_CALLBACK_REQUEST, INTENT_HUMAN_REQUEST):
        if wanted in intents:
            return wanted
    return first_substantive_intent(user_messages)[0]


def classify_ticket(user_messages, final_action="CREATE_TICKET", ticket_reason=None,
                    data_available=None, tool_available=True, model_reason=None,
                    model_available=True):
    """Why a conversation ended in a ticket, kept apart from what the customer
    last clicked: ``original_intent`` (what they came for), ``final_action``,
    ``ticket_reason`` (the subject the ticket was filed under) and
    ``ai_failure_reason`` / ``escalation_reason`` (whether the AI, not the
    customer, caused the handover)."""
    msgs = [m for m in (user_messages or []) if m and m.strip()]
    original, oconf = first_substantive_intent(msgs)
    first_meaningful = next((m for m in msgs if is_meaningful(m)), msgs[0] if msgs else "")
    lang = detect(first_meaningful)
    failure = None
    asked_person = original in (INTENT_HUMAN_REQUEST, INTENT_CALLBACK_REQUEST) or ticket_reason in (
        INTENT_HUMAN_REQUEST, INTENT_CALLBACK_REQUEST)
    if asked_person:
        escalation = ESC_CUSTOMER_REQUESTED_HUMAN
    elif not model_available:
        escalation, failure = ESC_MODEL_UNAVAILABLE, FAIL_MODEL_UNAVAILABLE
    elif model_reason in ESCALATION_REASONS:
        escalation = model_reason
    elif original in ANSWERABLE_INTENTS:
        if not tool_available:
            escalation, failure = ESC_TOOL_UNAVAILABLE, FAIL_NO_TOOL
        elif data_available is False:
            escalation = ESC_DATA_MISSING
        elif lang["language"] not in ("en", "und"):
            escalation, failure = ESC_LANGUAGE_UNDERSTANDING_FAILED, FAIL_NLU_LANGUAGE
        else:
            escalation, failure = ESC_AI_LOW_CONFIDENCE, FAIL_LOW_CONFIDENCE
    else:
        escalation = ESC_UNCLASSIFIED
    if escalation == ESC_LANGUAGE_UNDERSTANDING_FAILED and failure is None:
        failure = FAIL_NLU_LANGUAGE
    return {"original_intent": original, "original_intent_confidence": oconf,
            "final_action": final_action, "ticket_reason": ticket_reason,
            "escalation_reason": escalation, "ai_failure_reason": failure,
            "detected_language": lang["language"], "script": lang["script"],
            "code_mixed": lang["code_mixed"]}


def legacy_ticket_reason(subject):
    s = (subject or "").lower()
    if "callback" in s or "call me" in s or "call before" in s:
        return "CUSTOMER_CALLBACK"
    if "prescription" in s:
        return "PRESCRIPTION_QUESTION"
    if "order frames" in s:
        return "ORDER_ENQUIRY"
    return "OTHER"


# ─── the model's own statement of the turn ───

_META_RE = re.compile(r"\[\s*META\s*:([^\]]*)\]", re.I)
_META_KEYS = ("intent", "lang", "clarify", "esc")


def extract_meta(reply):
    """Strip every ``[META:k=v;...]`` tag from a model reply; return
    ``(reply, meta)`` where meta holds only recognised keys with valid values.
    The tag is never shown to a customer, whatever it contains."""
    if not reply:
        return reply, {}
    meta = {}
    for m in _META_RE.finditer(reply):
        for part in m.group(1).split(";"):
            if "=" not in part:
                continue
            k, v = (x.strip() for x in part.split("=", 1))
            k = k.lower()
            if k not in _META_KEYS or not v:
                continue
            if k == "intent" and v.upper() in INTENTS:
                meta["intent"] = v.upper()
            elif k == "esc" and v.upper() in ESCALATION_REASONS:
                meta["esc"] = v.upper()
            elif k == "lang" and v in LANGUAGE_NAMES:
                meta["lang"] = v
            elif k == "clarify":
                meta["clarify"] = v in ("1", "true", "yes")
    cleaned = _META_RE.sub("", reply)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned).strip()
    return cleaned, meta


def prompt_section(understanding):
    """The per-turn language/intent note the conversation model reads."""
    lang = understanding.get("detected_language") or "und"
    name = LANGUAGE_NAMES.get(lang, lang)
    mixed = " (mixed with English words)" if understanding.get("code_mixed") else ""
    intent = understanding.get("intent") or INTENT_OTHER
    lines = ["", "THIS TURN (detected by the server, not by you):",
             "  Customer's language: %s%s, script %s, confidence %.2f."
             % (name, mixed, understanding.get("script") or "-",
                float(understanding.get("language_confidence") or 0))]
    if intent not in (INTENT_OTHER, INTENT_GREETING):
        lines.append("  Likely intent: %s (confidence %.2f). Confirm it from the "
                     "message yourself; if it is wrong, follow the message."
                     % (intent, float(understanding.get("intent_confidence") or 0)))
    return "\n".join(lines) + "\n"


LANGUAGE_RULES = """
LANGUAGE AND UNDERSTANDING (applies to every reply):
L1. Reply in the language AND script of the customer's most recent meaningful message. Hinglish (Hindi in Roman letters, e.g. "mera chashma kis number ka banega") gets a Hinglish reply in Roman letters; Hindi in Devanagari gets Devanagari; Tamil, Bengali, Punjabi, Marathi, Gujarati, Telugu, Kannada, Malayalam, Odia, Urdu and the other Indian languages get a reply in that language. Never ask the customer to choose a language, and do not switch to English because of one English word or a greeting. If the customer switches language, switch with them.
L2. Understand optical words in any language or spelling: "chashma/chashme/chasma/specs/ainak" = spectacles; "chashme ka number", "aankh ka number", "power", "number" next to spectacles/eyes = the spectacle prescription (SPH/CYL/AXIS/ADD); "door ka number" = distance power, "paas/nazdeek ka number" = reading/near power; "number add kiya / daal diya / save kiya" = the customer already entered their prescription. Spelling mistakes, missing punctuation and voice-typing errors are normal; read for meaning.
L3. When the customer asks which power their glasses will be made with, or whether their prescription was saved, answer from PRESCRIPTIONS ON FILE when that section is present: glasses are made with exactly the prescription attached to that cart line or order. Quote the values exactly as listed; never guess, round, calculate or invent a value. If the section says nothing is on file, say so plainly and ask ONE specific question (e.g. which order, or whether they entered it on the product page).
L4. If you are not sure what the customer means, ask ONE short, specific clarification in their language that restates what you think they asked (e.g. "Aap yeh pooch rahe hain ki jo prescription aapne save kiya hai, glasses usi power ke banenge — sahi?"). Never answer an unclear question with a menu of unrelated categories. Offer a support ticket only after a clarification did not resolve it, or when the customer asks for a person.
L5. In the yes/no questions of rules 8 and 9, translate the question into the customer's language but keep the words "supervisor" and "support ticket" in Roman letters so the server recognises the question.
L6. End EVERY reply with one hidden tag, on its own, exactly: [META:intent=<INTENT>;lang=<code>;clarify=<0|1>;esc=<REASON or empty>]. INTENT is one of PRESCRIPTION_CONFIRMATION, PRESCRIPTION_STATUS, PRESCRIPTION_FOR_ORDER, PRESCRIPTION_ENTRY_HELP, ORDER_STATUS, RESHIP_STATUS, PAYMENT_STATUS, HUMAN_REQUEST, CALLBACK_REQUEST, PRODUCT_SEARCH, GREETING, OTHER. lang is the code of the language you replied in (en, hi, hi-Latn, bn, ta, te, mr, gu, pa, kn, ml, or, ur, as, ne, ...). clarify=1 when this reply asks a clarifying question. esc is set only when you add [ACTION:HUMAN_HANDOVER] or [ACTION:CREATE_TICKET]: CUSTOMER_REQUESTED_HUMAN, LANGUAGE_UNDERSTANDING_FAILED, TOOL_UNAVAILABLE, DATA_MISSING, POLICY_REQUIRES_HUMAN or AI_LOW_CONFIDENCE. The server removes the tag; the customer never sees it.
"""


# ─── what the server itself says, in the customer's language ───
# Replies the server decides (a callback or a person was asked for, the model
# is down, an account question from a signed-out browser) are written here so
# they never depend on the model and never answer Hinglish in English.

SUPPORT_REPLIES = {
    "callback_ticket": {
        "en": "{lead}I've asked our support team to call you back on the number ending "
              "{last4}. They usually call within 24 hours.",
        "hi-Latn": "{lead}Maine support team ko aapko {last4} par khatam hone wale number "
                   "par call karne ki request bhej di hai. Woh aam taur par 24 ghante ke "
                   "andar call karte hain.",
        "hi": "{lead}मैंने सपोर्ट टीम को आपको {last4} पर ख़त्म होने वाले नंबर पर कॉल करने का "
              "अनुरोध भेज दिया है। वे आम तौर पर 24 घंटे के अंदर कॉल करते हैं।",
    },
    "human_ticket": {
        "en": "{lead}I've passed this conversation to our support team. A person will "
              "reply to you by email within 24 hours.",
        "hi-Latn": "{lead}Maine yeh baatcheet hamari support team ko bhej di hai. Team ka "
                   "ek vyakti 24 ghante ke andar aapko email par jawab dega.",
        "hi": "{lead}मैंने यह बातचीत हमारी सपोर्ट टीम को भेज दी है। टीम का एक व्यक्ति 24 घंटे "
              "के अंदर आपको ईमेल पर जवाब देगा।",
    },
    "ask_contact_callback": {
        "en": "I can ask our support team to call you back. Please type your mobile "
              "number and email address here, or sign in, and I'll create the callback "
              "request.",
        "hi-Latn": "Main support team se aapko call karwa sakta hoon. Apna mobile number "
                   "aur email yahan likh dijiye, ya sign in kijiye, main callback request "
                   "bana dunga.",
        "hi": "मैं सपोर्ट टीम से आपको कॉल करवा सकता हूँ। अपना मोबाइल नंबर और ईमेल यहाँ "
              "लिखिए, या साइन इन कीजिए, मैं कॉलबैक अनुरोध बना दूँगा।",
    },
    "ask_contact_human": {
        "en": "I can pass this to a person on our support team. Please type your email "
              "address here, or sign in, and I'll send them this conversation.",
        "hi-Latn": "Main aapki baat support team ke kisi vyakti tak pahuncha sakta hoon. "
                   "Apna email yahan likh dijiye, ya sign in kijiye, main unhe yeh "
                   "baatcheet bhej dunga.",
        "hi": "मैं आपकी बात सपोर्ट टीम के किसी व्यक्ति तक पहुँचा सकता हूँ। अपना ईमेल यहाँ "
              "लिखिए, या साइन इन कीजिए, मैं उन्हें यह बातचीत भेज दूँगा।",
    },
    "ask_phone": {
        "en": "I can ask our support team to call you back. Which mobile number should "
              "they call?",
        "hi-Latn": "Main support team se aapko call karwa sakta hoon. Kis mobile number "
                   "par call karein?",
        "hi": "मैं सपोर्ट टीम से आपको कॉल करवा सकता हूँ। किस मोबाइल नंबर पर कॉल करें?",
    },
    "ticket_ref": {
        "en": "Your request number is {ref}.",
        "hi-Latn": "Aapka request number {ref} hai.",
        "hi": "आपका अनुरोध नंबर {ref} है।",
    },
    "sign_in_account": {
        "en": "Your orders, payments, saved prescriptions and returned parcels are shown "
              "after you sign in. Sign in and open My Orders.",
        "hi-Latn": "Aapke orders, payment, saved power aur return hue parcel sign in karne "
                   "ke baad dikhte hain. Sign in karke My Orders kholiye.",
        "hi": "आपके ऑर्डर, पेमेंट, सेव किया हुआ पावर और लौटे हुए पार्सल साइन इन करने के बाद "
              "दिखते हैं। साइन इन करके My Orders खोलिए।",
    },
    "model_down_orders": {
        "en": "I can't read your orders this moment. They are all in My Orders, or ask "
              "me again in a minute.",
        "hi-Latn": "Main abhi aapke orders nahi padh pa raha. Woh sab My Orders mein hain, "
                   "ya ek minute baad dobara poochhiye.",
        "hi": "मैं अभी आपके ऑर्डर नहीं पढ़ पा रहा। वे सब My Orders में हैं, या एक मिनट बाद "
              "फिर से पूछिए।",
    },
    "model_down_products": {
        "en": "I can't search the catalogue this moment. You can browse all frames, or "
              "ask me again in a minute.",
        "hi-Latn": "Main abhi catalogue search nahi kar pa raha. Aap saare frames dekh "
                   "sakte hain, ya ek minute baad dobara poochhiye.",
        "hi": "मैं अभी कैटलॉग नहीं खोज पा रहा। आप सारे फ़्रेम देख सकते हैं, या एक मिनट बाद "
              "फिर से पूछिए।",
    },
    "model_down_retry": {
        "en": "I can't answer that this moment. Please ask me again in a minute.",
        "hi-Latn": "Main abhi iska jawab nahi de pa raha. Kripya ek minute baad dobara "
                   "poochhiye.",
        "hi": "मैं अभी इसका जवाब नहीं दे पा रहा। कृपया एक मिनट बाद फिर से पूछिए।",
    },
}
SUPPORT_REPLY_LANGUAGES = ("en", "hi-Latn", "hi")


def support_reply(kind, language, **values):
    """``(text, written_in)``: the server's own reply of ``kind`` in the
    customer's language where one is written, else English (``written_in``
    then says so, and the caller may have it translated)."""
    texts = SUPPORT_REPLIES[kind]
    lang = language if language in texts else "en"
    fill = {"lead": "", "last4": "", "ref": ""}
    fill.update(values)
    return texts[lang].format(**fill).strip(), lang
