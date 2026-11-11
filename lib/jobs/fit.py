"""
How well a job fits you, by keyword rules (plain code, no Claude). Used to rank the job archive.
Weights: strong boost +3, core area +2, boost +1, penalty -2. Defaults retuned on 2026-11-10 so
that with a minimum fit of 2 a plain SWE internship or any quant/trading role shows, other areas
need two matches, and Bachelor's no longer counts (the CSCareers list has no degree info, so it
only ever helped Simplify postings). Change weights or add keywords from the archive's ⚙️ Filters
screen (saved in archive.json).
"""

import functools
import re

# (label, weight, pattern, where): `where` is "title" for words that only mean something in the
# job title (a description saying "full-time return offer" isn't a full-time job), else "any".
# Short acronyms are matched case-sensitively so "AI" doesn't match "ai" inside other words' text.
KEYWORDS = [
    ("Quant / trading", 2, r"(?i:\bquant(?:itative)?\b|\btrad(?:ing|er|ers)\b)", "any"),
    ("Software engineering", 2, r"(?i:\bsoftware (?:engineer(?:ing)?|development|developer)\b)|\bSWE\b|\bSDE\b", "any"),
    ("Data science", 1, r"(?i:\bdata scien(?:ce|tist)\b)", "any"),
    ("ML / AI", 1, r"(?i:\bmachine learning\b|\bdeep learning\b)|\bML\b|\bAI\b", "any"),
    ("Embedded", 1, r"(?i:\bembedded\b|\bfirmware\b|\bmicrocontrollers?\b)", "any"),
    ("Hardware", 1, r"(?i:\belectrical engineer(?:ing)?\b|\bhardware\b)|\bFPGA\b|\bASIC\b|\bVLSI\b", "any"),
    ("Signals / wireless", 1, r"(?i:\bsignal processing\b|\bwireless\b|\bcommunications\b)|\bDSP\b|\bRF\b", "any"),
    ("C++", 1, r"(?i:c\+\+|\blow[- ]latency\b|\bhigh[- ]performance\b)", "any"),
    ("Market making / options", 1, r"(?i:\bmarket[- ]mak(?:ing|er)\b|\boptions\b|\bderivatives\b)", "any"),
    ("Finance", 1, r"(?i:\bhedge fund\b|\basset management\b|\binvestments?\b|\bfintech\b|\bpayments\b)", "any"),
    ("Research", 1, r"(?i:\bresearch(?:er)?\b|\bR&D\b)", "any"),
    ("Cloud / infra", 1, r"(?i:\bcloud\b|\binfrastructure\b|\bdevops\b)|\bSRE\b", "any"),
    ("Bachelor's", 0, r"(?i:\bbachelor'?s?\b|\bundergrad(?:uate)?\b)|\bB\.?S\.?\b|\bB\.?A\.?\b", "any"),
    # Any graduate degree, unless the posting is also open to undergrads (see EXCEPT).
    ("Grad degree", -2, r"(?i:\bph\.?ds?\b|\bdoctoral\b|\bmaster'?s\b|\bgraduate (?:students?|degree|program|level)\b"
                        r"|\bgrad students?\b)|\bMBA\b", "any"),
    ("Senior / full-time", -2, r"(?i:\bsenior\b|\bstaff\b|\bprincipal\b|\bnew grad\b|\bfull[- ]time\b)", "title"),
    ("Sales / marketing", -2, r"(?i:\bsales\b|\bmarketing\b|\baccount manager\b|\brecruit(?:ing|er)\b)", "title"),
    ("Off-target", -2, r"(?i:\bbusiness analy|\bsupply chain\b|\bhuman resources\b|\btalent\b|\baccounting\b"
                       r"|\bmechanical\b|\bcivil\b|\bmanufacturing\b|\bprocurement\b|\bhelp ?desk\b|\btechnician\b"
                       r"|\bIT support\b)|\bHR\b", "title"),
]
COMPILED = [(label, weight, re.compile(pattern), where) for label, weight, pattern, where in KEYWORDS]
# A keyword that doesn't count when its exception also matches the posting.
EXCEPT = {"Grad degree": re.compile(r"(?i)\bundergrad|\bbachelor"),
          "Off-target": re.compile(r"(?i)\bsoftware (?:engineer|develop)")}   # "SWE Intern, HR Data"
WEIGHTS = (3, 2, 1, -2, 0)             # what tapping a keyword in the archive cycles through; 0 = off
MAX_KEYWORDS = 24                      # what fits on the archive's keyword screen


@functools.lru_cache(maxsize=64)
def _words_rx(words: tuple[str, ...]) -> re.Pattern:
    """A keyword you added in the archive: any of its words, case-insensitive, as whole words."""
    return re.compile(r"(?i)(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)")


def keywords(settings: dict | None = None) -> list[tuple[str, int, re.Pattern, str]]:
    """The keywords in use: the built-in ones with any weights you changed in the archive's
    settings, then the ones you added there. Weight 0 means turned off."""
    s = settings or {}
    weights = s.get("weights", {})
    out = [(label, weights.get(label, weight), rx, where) for label, weight, rx, where in COMPILED]
    out += [(c["label"], c["weight"], _words_rx(tuple(c["words"])), "any") for c in s.get("custom", [])]
    return out


def score(job: dict, description: str = "", kws: list | None = None) -> tuple[int, list[str]]:
    """(fit score, matched labels like "+3 Quant") for a job dict with title/company/location.
    `kws`: from keywords(settings); the built-in weights if left out."""
    title = job.get("title") or ""
    text = " ".join([title, job.get("company") or "", description])
    total, matched = 0, []
    for label, weight, rx, where in (kws if kws is not None else COMPILED):
        if (weight and rx.search(title if where == "title" else text)
                and not (label in EXCEPT and EXCEPT[label].search(text))):
            total += weight
            matched.append(f"{weight:+d} {label}")
    return total, matched
