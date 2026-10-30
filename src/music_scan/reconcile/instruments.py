"""Map staff label text to a General MIDI program and a transposition.

Labels come straight from the PDF ("Trumpet in Bb", "Tpt. II", "Hn. (F)", "S / A", "Klarinette in B"),
so matching works on normalized tokens and a table of names and abbreviations in English plus a
few Italian and German ones. Programs are 1-based, as MusicXML's `midi-program` counts them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

STEPS = "CDEFGAB"
_NATURAL = (0, 2, 4, 5, 7, 9, 11)


@dataclass(frozen=True)
class Transposition:
    """MusicXML `<transpose>`: sounding pitch = written pitch + this interval."""

    diatonic: int
    chromatic: int


@dataclass(frozen=True)
class InstrumentInfo:
    name: str
    program: int
    transpose: Transposition | None = None
    # choir voices: "upper" (soprano, alto, women), "lower" (tenor, bass, men), "mixed"
    voice: str | None = None


@dataclass(frozen=True)
class _Family:
    name: str
    pattern: re.Pattern[str]
    program: int
    # (low, high]: the sounding offset of written C for a named key lands in this range of
    # semitones, which fixes the octave (a Bb trumpet is -2, a Bb tenor sax -14)
    key_range: tuple[int, int] | None = None
    # key assumed when the label names none; only for instruments always written transposed
    default_key: str | None = None
    voice: bool = False


def _f(name: str, words: str, program: int, key_range=None, default_key=None, voice=False) -> _Family:
    return _Family(name, re.compile(rf"(?:^| )(?:{words})(?= |$)"), program, key_range, default_key, voice)


# Order matters: compound names come before the words they contain ("bass clarinet" before
# "clarinet" and the voice "bass", "english horn" before "horn").
_FAMILIES: tuple[_Family, ...] = (
    _f("Bass Clarinet", r"bass clarinet|b cl|bcl|bass cl|bassklarinette|clarinetto basso|clarone", 72, (-24, -12), "Bb"),
    _f("Bass Trombone", r"bass trombone|b tbn|bass tbn|btbn|bassposaune", 58),
    _f("Bass Guitar", r"bass guitar|electric bass|e bass|el bass|bass gtr|bgtr", 34),
    _f("Double Bass", r"double bass|string bass|contrabass|contra bass|kontrabass|contrabbasso|"
                      r"upright bass|d b|db|cb|kb|cbs", 44),
    _f("Soprano Saxophone", r"soprano sax(?:ophone)?|sop sax|s sax|ssax", 65, (-6, 6), "Bb"),
    _f("Alto Saxophone", r"alto sax(?:ophone)?|a sax|asax|alt sax|altsaxophon", 66, (-12, 0), "Eb"),
    _f("Tenor Saxophone", r"tenor sax(?:ophone)?|t sax|tsax|ten sax|tenorsaxophon", 67, (-24, -12), "Bb"),
    _f("Baritone Saxophone", r"baritone sax(?:ophone)?|bari sax|bar sax|b sax|bsax|baritonsaxophon", 68, (-24, -12), "Eb"),
    _f("English Horn", r"english horn|eng hn|e hn|cor anglais|corno inglese|englischhorn", 70, (-12, 0), "F"),
    _f("Flugelhorn", r"flugelhorn|flugel|flghn|flgh", 57, (-6, 6)),
    _f("Alto Flute", r"alto flute|a fl", 74, (-12, 0), "G"),
    _f("Piccolo", r"piccolo|picc|pic|ottavino|kleine flote", 73),
    _f("Flute", r"flutes?|fl|flt|fls|flauto|flauti|flote|floten|flute traversiere", 74),
    _f("Oboe", r"oboes?|ob|obs|oboi|hoboe", 69),
    _f("Clarinet", r"clarinets?|cl|cla|clar|clars|klar|klarinette|klarinetten|clarinetto|clarinetti", 72, (-6, 6)),
    _f("Bassoon", r"bassoons?|bsn|bn|bsns|fg|fag|fagott|fagotte|fagotto|fagotti|basson", 71),
    _f("Saxophone", r"saxophone|sax", 66, (-12, 0)),
    _f("Trumpet", r"trumpets?|tpt|tpts|tp|trp|trpt|tromba|trombe|trompete|trompeten|trompette", 57, (-6, 6)),
    _f("Cornet", r"cornets?|cnt|cor net|kornett", 57, (-6, 6)),
    _f("Horn", r"french horn|horns?|hn|hns|hrn|cor|corno|corni|horner|waldhorn", 61, (-12, 0)),
    _f("Trombone", r"trombones?|tbn|tbns|trb|trbn|tromb|posaune|posaunen", 58),
    _f("Euphonium", r"euphonium|euph|baritone horn|bar hn|tenorhorn", 59),
    _f("Tuba", r"tubas?|tba", 59),
    _f("Timpani", r"timpani|timp|tim|timps|kettledrums?|pauken|pk|timbales", 48),
    _f("Glockenspiel", r"glockenspiel|glock|glk|orchestra bells|bells", 10),
    _f("Xylophone", r"xylophone|xylo|xyl", 14),
    _f("Marimba", r"marimba|mar|mrb", 13),
    _f("Vibraphone", r"vibraphone|vibes|vib|vibr", 12),
    _f("Chimes", r"chimes|tubular bells|chm|campane", 15),
    _f("Celesta", r"celesta|cel|celeste", 9),
    _f("Harpsichord", r"harpsichord|hpsd|hpd|cembalo|clavecin", 7),
    _f("Organ", r"organ|org|organo|orgel|orgue|ped|pedal|pedals", 20),
    _f("Piano", r"piano|pno|pf|pianoforte|klavier|kl|pianoforte", 1),
    _f("Accordion", r"accordion|acc|akkordeon|fisarmonica", 22),
    _f("Harp", r"harp|hp|hrp|arpa|harfe|harpe", 47),
    _f("Guitar", r"guitar|gtr|guit|gitarre|chitarra|guitare|acoustic guitar|electric guitar", 25),
    _f("Violin", r"violins?|vln|vlns|vn|vns|vl|vi|vni|violino|violini|violine|violinen|geige|violon", 41),
    _f("Viola", r"violas?|vla|vlas|va|vle|bratsche|bratschen|alto viola", 42),
    _f("Cello", r"cellos?|violoncello|violoncelli|vc|vcs|vcl|vlc|celli|violoncelle", 43),
    _f("Recorder", r"recorder|rec|blockflote|flauto dolce", 75),
    _f("Choir", r"sopranos?|sopr|sop|sopran|altos?|alt|contralto|mezzo|mezzo soprano|tenors?|ten|tenore|"
                r"bass|basses|basso|bassi|baritone|bar|baritono|bariton|women|womens|ladies|female|treble|"
                r"trebles|girls|boys|men|mens|gentlemen|male|choir|chorus|chor|coro|choeur|voice|voices|"
                r"vocal|vocals|vox|unison|satb|ssa|ssaa|ttb|ttbb|sa|tb|sab|s|a|t|b", 53, voice=True),
)

_UPPER = {"soprano", "sopranos", "sopr", "sop", "sopran", "alto", "altos", "alt", "contralto", "mezzo",
          "women", "womens", "ladies", "female", "treble", "trebles", "girls", "boys", "ssa", "ssaa", "sa",
          "s", "a"}
_LOWER = {"tenor", "tenors", "ten", "tenore", "bass", "basses", "basso", "bassi", "baritone", "bar",
          "baritono", "bariton", "men", "mens", "gentlemen", "male", "ttb", "ttbb", "tb", "t", "b"}

# a key name after "in", in parentheses, or leading the label: "in Bb", "(F)", "Eb Alto Sax", "in Es"
_ACC = r"(?: ?(?:flat|b|sharp|#|es|s))?"
_KEY = re.compile(rf"(?:^|\bin |\()([a-g])({_ACC})(?=\)| |$)")
_KEY_WORDS = re.compile(rf"\bin [a-g]{_ACC}(?= |\)|$)|\([a-g]{_ACC}\)")
_SOLFEGE = {"do": "C", "re": "D", "mi": "E", "fa": "F", "sol": "G", "la": "A", "si": "B"}
_KEY_SOLFEGE = re.compile(r"\bin (do|re|mi|fa|sol|la|si)( ?b|bemolle| bem)?(?= |\)|$)")
_NUMBERING = re.compile(r"^(?:\d+|[ivx]+|\d+(?:st|nd|rd|th))$")
_GERMAN = re.compile(r"klarinette|trompete|horner|waldhorn|posaune")


def normalize(label: str, keep_parens: bool = False) -> str:
    text = label.replace("♭", "b").replace("♯", "#").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9#()]+" if keep_parens else r"[^a-z0-9#]+", " ", text)
    return " ".join(text.split())


def _key(text: str, german: bool) -> tuple[str, int, bool] | None:
    """(step, alter, bare leading letter) of a named key in parenthesis-keeping text, or None."""
    if (m := _KEY_SOLFEGE.search(text)) is not None:
        return _SOLFEGE[m.group(1)], -1 if m.group(2) else 0, False
    for m in _KEY.finditer(text):
        step, acc = m.group(1).upper(), m.group(2).strip()
        if acc in ("b", "flat", "es") or (acc == "s" and step in "AE"):
            alter = -1
        elif acc in ("#", "sharp"):
            alter = 1
        elif acc == "":
            alter = 0
        else:
            continue
        # German B is B-flat ("Klarinette in B"); H is B natural
        if german and step == "B" and alter == 0:
            alter = -1
        return step, alter, m.start() == 0 and not acc
    return None


def transposition(step: str, alter: int, key_range: tuple[int, int]) -> Transposition | None:
    """The `<transpose>` of an instrument in `step`+`alter`, its octave fixed by `key_range`."""
    semis = _NATURAL[STEPS.index(step)] + alter
    lo, hi = key_range
    octave = 0
    while semis + 12 * octave <= lo:
        octave += 1
    while semis + 12 * octave > hi:
        octave -= 1
    chromatic = semis + 12 * octave
    if chromatic == 0:
        return None
    return Transposition(STEPS.index(step) + 7 * octave, chromatic)


def _words(label: str) -> list[str]:
    text = _KEY_SOLFEGE.sub(" ", _KEY_WORDS.sub(" ", normalize(label, keep_parens=True)))
    return [w for w in normalize(text).split() if not _NUMBERING.match(w) and w != "group"]


def _family(words: list[str]) -> _Family | None:
    body = " ".join(words)
    for fam in _FAMILIES:
        if not fam.pattern.search(body):
            continue
        # every word must be a voice word, so "A Tune" or "Solo Trumpet" never reads as a choir
        if fam.voice and not all(fam.pattern.search(w) for w in words):
            continue
        return fam
    return None


def lookup(label: str) -> InstrumentInfo | None:
    """The instrument a staff label names, or None when it names none the table knows."""
    words = _words(label)
    fam = _family(words)
    if fam is None:
        return None
    if fam.voice:
        return InstrumentInfo(fam.name, fam.program, None, voice_register(label))
    tr = None
    if fam.key_range is not None:
        text = normalize(label, keep_parens=True)
        key = _key(text, bool(_GERMAN.search(text)))
        # a bare leading letter is a key only when the rest names the same instrument
        # ("F Horn"), not when it is part of an abbreviation ("A Sax", "B Cl")
        if key is not None and key[2] and _family(words[1:]) is not fam:
            key = None
        if key is None and fam.default_key:
            key = (fam.default_key[0], -1 if fam.default_key.endswith("b") else 0, False)
        if key is not None:
            tr = transposition(key[0], key[1], fam.key_range)
    return InstrumentInfo(fam.name, fam.program, tr)


def voice_register(label: str) -> str | None:
    """"upper", "lower" or "mixed" for a choir label; None when it isn't one."""
    words = _words(label)
    fam = _family(words)
    if fam is None or not fam.voice:
        return None
    up = any(w in _UPPER for w in words)
    low = any(w in _LOWER for w in words)
    if up != low:
        return "upper" if up else "lower"
    return "mixed"


def _explained(words: list[str], fam: _Family) -> bool:
    """Every word belongs to the family's name, apart from a leading key letter ("F Horn")."""
    rest = " ".join(words)
    while True:
        nxt = " ".join(fam.pattern.sub(" ", rest).split())
        if nxt == rest:
            break
        rest = nxt
    return rest == "" or (len(rest) == 1 and rest in "abcdefg" and words[0] == rest)


def find_in(text: str) -> tuple[str, InstrumentInfo] | None:
    """The longest run of words in `text` (a title line or file name) that is nothing but an
    instrument name, with its key and numbering."""
    words = [w for w in text.replace("_", " ").split() if any(ch.isalnum() for ch in w)]
    best: tuple[int, int, str, InstrumentInfo] | None = None
    for i in range(len(words)):
        for j in range(i + 1, len(words) + 1):
            span = " ".join(words[i:j])
            ws = _words(span)
            fam = _family(ws)
            if fam is None or fam.voice or not _explained(ws, fam):
                continue
            info = lookup(span)
            cand = (j - i, -i, span, info)
            if info is not None and (best is None or cand[:2] > best[:2]):
                best = cand
    return None if best is None else (best[2], best[3])


__all__ = ["InstrumentInfo", "Transposition", "find_in", "lookup", "normalize", "transposition", "voice_register"]
