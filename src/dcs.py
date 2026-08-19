"""DCS (Digital-Coded Squelch) codeword handling.

DCS is a 23-bit word repeated continuously under the voice at 134.4 bps, sent as
low-deviation subaudible FSK. `survey_prototype.analyze_analog` could already tell
that *something* DCS-like was present but had no codeword, so a channel using it
stopped at tier 2 — "probably some DCS" will not programme a radio. This decodes it.

The word is a binary Golay(23,12) code: 12 information bits and 11 check bits. The
code is *perfect*, so every one of the 2^11 syndromes corresponds to exactly one
error pattern of weight <= 3, and correction is a table lookup with no ambiguity.
That property is what makes decoding reliable on a signal buried 20 dB under voice.

Of the 12 information bits, 9 carry the code as three octal digits and 3 are fixed.
The fixed triple is a free correctness check: a random 23-bit window that happens to
be a valid Golay codeword still fails it 7 times out of 8.

    The word is [3 fixed bits][9 code bits][11 parity bits], most significant
    first. The fixed triple is octal digit 4, so the twelve information bits read
    as a four-digit octal number whose last three digits are the code.

    Verified against a real off-air decode. DCS 023 is

        100 000010011 11101100011

    and this module reproduces it exactly. The same check settled the generator
    polynomial, which is not guessable: 0xC75 and its reciprocal 0xAE3 both
    generate a perfect binary Golay code and both pass every internal consistency
    test this file can run, but only one is the code DCS uses. This module had the
    wrong one until the off-air word was checked against it, and everything it
    produced before that was a valid codeword of the wrong code.

    On framing. A DCS stream is one word repeating forever with no sync pattern,
    the code is cyclic, and all-ones is a codeword - so every rotation and every
    complement is also a valid codeword, and blind decoding looks hopeless. The
    restricted code list resolves it: across the standard codes every waveform has
    exactly two legal readings, one normal and one inverted, and they are the same
    signal. Transmitting 023 normal *is* transmitting 047 inverted, and a radio set
    to either opens on it. That is the inverted-code pairing radio documentation
    lists; INVERTED_PAIR derives it from the codewords rather than transcribing it,
    and asserts on import that every code pairs cleanly. The decoder reports the
    normal reading, which every waveform has exactly one of.
    """

from __future__ import annotations

import itertools

# g(x) = x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1.
#
# Both this and its reciprocal 0xAE3 generate *a* binary Golay code, and either
# will pass the perfect-code check below — which is why picking the wrong one is
# not self-evident. DCS uses this one. Verified against a decoded off-air
# example: DCS 023 is the 23-bit word
#
#     100 000010011 11101100011
#     ^   ^         ^
#     |   |         11 parity bits
#     |   9 code bits, 000010011 = 0o023
#     3 fixed bits, octal digit 4
#
# 0xAE3 yields parity 11111101000 for that data and is simply a different code.
GOLAY_POLY = 0xC75

WORD_BITS = 23
DATA_BITS = 12
CODE_BITS = 9
FIXED_TRIPLE = 0b100        # information bits 9..11

BPS = 134.4                 # bit rate; the word therefore repeats at 5.84 Hz

# The standard three-digit octal codes, from the RadioReference DCS table.
#
# Only these are legal; the field is nine bits, so 512 values encode but a radio
# will not let you dial in the rest. That restriction is load-bearing rather than
# cosmetic — it is most of what stops a window of noise decoding to something
# plausible.
STANDARD_CODES = (
    "006", "007", "015", "017", "021", "023", "025", "026", "031", "032",
    "036", "043", "047", "050", "051", "053", "054", "065", "071", "072",
    "073", "074", "114", "115", "116", "122", "125", "131", "132", "134",
    "141", "143", "145", "152", "155", "156", "162", "165", "172", "174",
    "205", "212", "214", "223", "225", "226", "243", "244", "245", "246",
    "251", "252", "255", "261", "263", "265", "266", "271", "274", "306",
    "311", "315", "325", "331", "332", "343", "346", "351", "356", "364",
    "365", "371", "411", "412", "413", "423", "431", "432", "445", "446",
    "452", "454", "455", "462", "464", "465", "466", "503", "506", "516",
    "523", "526", "532", "546", "565", "606", "612", "624", "627", "631",
    "632", "654", "662", "664", "703", "712", "723", "731", "732", "734",
    "743", "754"
)


def _mod_g(v: int) -> int:
    """Remainder of v as a GF(2) polynomial, modulo the Golay generator."""
    for i in range(WORD_BITS - 1, DATA_BITS - 2, -1):
        if v & (1 << i):
            v ^= GOLAY_POLY << (i - (DATA_BITS - 1))
    return v & 0x7FF


def golay_encode(data12: int) -> int:
    """12 information bits -> 23-bit systematic codeword."""
    shifted = (data12 & 0xFFF) << 11
    return shifted | _mod_g(shifted)


def _build_syndrome_table() -> dict[int, int]:
    """Syndrome -> coset leader, for every error pattern of weight <= 3.

    The Golay code is perfect: sum(C(23,k) for k in 0..3) == 2048 == 2^11, so this
    table is exactly complete with no collisions. The assert below is not
    decoration — it is the property the decoder's correctness rests on.
    """
    table: dict[int, int] = {}
    for weight in range(4):
        for bits in itertools.combinations(range(WORD_BITS), weight):
            err = 0
            for b in bits:
                err |= 1 << b
            syn = _mod_g(err)
            assert syn not in table, "Golay code is not perfect — generator is wrong"
            table[syn] = err
    assert len(table) == 2048, f"expected 2048 cosets, built {len(table)}"
    return table


_SYNDROMES = _build_syndrome_table()


def golay_decode(word23: int) -> tuple[int, int]:
    """23-bit received word -> (12 information bits, bit errors corrected)."""
    err = _SYNDROMES[_mod_g(word23 & 0x7FFFFF)]
    return ((word23 ^ err) >> 11) & 0xFFF, bin(err).count("1")


def encode(code: str) -> int:
    """Three-digit octal code -> the 23-bit word that goes on the air."""
    code9 = int(code, 8)
    if not 0 <= code9 < (1 << CODE_BITS):
        raise ValueError(f"code out of range: {code}")
    return golay_encode(code9 | (FIXED_TRIPLE << CODE_BITS))


def decode(word23: int, *, standard_only: bool = True) -> tuple[str, int] | None:
    """23-bit received word -> (octal code, bit errors), or None if it is not one.

    Rejects on two independent grounds: the fixed triple not surviving Golay
    correction, and the code not being one a radio can actually be set to. Both
    matter, because Golay correction maps *every* input to some codeword - a
    window of noise always "decodes" to something, and these two checks are what
    make it not count. Together they still let one through about 1 time in 37,
    which is why the stream decoder requires repeats to agree rather than
    trusting any single word.
    """
    data, nerr = golay_decode(word23)
    if (data >> CODE_BITS) != FIXED_TRIPLE:
        return None
    code = format(data & ((1 << CODE_BITS) - 1), "03o")
    if standard_only and code not in STANDARD_CODES:
        return None
    return code, nerr


def _inverted_pairs() -> dict[str, str]:
    """code -> the code whose INVERTED transmission is the same waveform.

    A DCS stream is one 23-bit word repeating forever with no sync pattern, so a
    receiver has to try all 23 rotations, and it must try both polarities because
    inverted DCS is a real thing radios send. The Golay code is cyclic, so every
    rotation of a codeword is a codeword, and the all-ones vector is a codeword,
    so every complement is one too. That sounds like hopeless ambiguity.

    It is not, and the reason is the restricted code list. Across the standard
    codes, every transmitted waveform presents exactly two legal readings: one
    normal and one inverted. Transmitting 023 normal *is* transmitting 047
    inverted — the same infinite bitstream, and a radio programmed to either will
    open on it. This is the "inverted codes" pairing that radio documentation
    lists, arrived at here from the codewords themselves.

    So the decoder is never ambiguous and never has to guess. It reports the
    normal reading, which every waveform has exactly one of.
    """
    all_ones = (1 << WORD_BITS) - 1
    seen: dict[int, set] = {}
    for code in STANDARD_CODES:
        word = encode(code)
        for polarity, base in (("N", word), ("I", word ^ all_ones)):
            for rot in range(WORD_BITS):
                rotated = ((base >> rot) | (base << (WORD_BITS - rot))) & all_ones
                seen.setdefault(rotated, set()).add((code, polarity))

    pairs = {}
    for code in STANDARD_CODES:
        word = encode(code)
        family = set()
        for rot in range(WORD_BITS):
            rotated = ((word >> rot) | (word << (WORD_BITS - rot))) & all_ones
            family |= seen.get(rotated, set())
        other = sorted(c for c, p in family if p == "I")
        if len(family) != 2 or len(other) != 1:
            raise AssertionError(
                f"{code} does not have exactly one normal and one inverted "
                f"reading: {sorted(family)} — the codeword table is wrong")
        pairs[code] = other[0]
    return pairs


INVERTED_PAIR: dict[str, str] = _inverted_pairs()


def bit_sequence(code: str, polarity: str = "N") -> list[int]:
    """The 23 bits as transmitted, LSB first. 'I' inverts, as an inverted DCS does.

    Note that `bit_sequence(c, "I")` is a rotation of
    `bit_sequence(INVERTED_PAIR[c], "N")` — the same waveform. That is a property
    of the standard, not of this implementation.
    """
    word = encode(code)
    bits = [(word >> k) & 1 for k in range(WORD_BITS)]
    if polarity == "I":
        bits = [1 - b for b in bits]
    elif polarity != "N":
        raise ValueError(f"polarity must be 'N' or 'I', got {polarity!r}")
    return bits


def _check() -> int:
    """Self-consistency of the codeword table. Every code must pair cleanly."""
    reference = int("100" "000010011" "11101100011", 2)
    ok = encode("023") == reference
    print(f"Golay syndrome table: {len(_SYNDROMES)} cosets (perfect code)")
    print(f"generator polynomial: 0x{GOLAY_POLY:X}")
    print(f"off-air check, DCS 023 word matches reference: {ok}")
    print(f"standard codes:       {len(STANDARD_CODES)}")
    print(f"normal/inverted pairs: {len(INVERTED_PAIR)} "
          f"(every waveform has exactly one reading of each polarity)")
    sample = ", ".join(f"{c}N={INVERTED_PAIR[c]}I" for c in list(STANDARD_CODES)[:4])
    print(f"  e.g. {sample}")
    return 0 if ok and len(INVERTED_PAIR) == len(STANDARD_CODES) else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_check())
