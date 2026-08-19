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

    THE CODEWORD TABLE HERE IS NOT THE REAL ONE. Read this before trusting a code.

    The Golay arithmetic below is correct and tested. What is *not* established is
    the mapping from a three-digit code to the 23 bits that go on the air.

    The problem is structural. Golay(23,12) is a **cyclic** code, so every one of
    the 23 rotations of a codeword is itself a valid codeword; the all-ones vector
    is also a codeword, so the bit-complement of a codeword is one too. A DCS
    transmission is the word repeated forever with no sync pattern, so a receiver
    sees an infinite periodic bitstream and has to pick the framing itself. Golay
    validity cannot do it — every framing is valid. Only the fixed triple and the
    list of legal codes can.

    That is not enough here. Under the convention implemented below, transmitting
    026 yields a bitstream that is also a legal framing of 311: the two are the
    same periodic sequence, so no receiver could ever separate them. A real
    standard cannot have that property, which means the real code set is chosen so
    that no two legal codes are rotations or complements of each other — and the
    construction below does not produce that set. Searching every plausible
    variation of the convention (fixed triple 0..7, LSB and MSB first, with and
    without polarity search) tops out at 70 of 104 codes framing uniquely.

    So `decode()` reports a code only when the framing is unambiguous, and the
    decoder in survey_prototype refuses to guess otherwise. It will report "DCS
    present, code unknown" — which is exactly what it did before — rather than a
    confident wrong answer. A wrong CTCSS/DCS code is worse than none: it sends
    you off to programme a radio that stays silent.

    TO FINISH THIS: replace CODEWORDS below with the real 23-bit word for each
    standard code, from a verified reference or measured off a radio transmitting
    a known code. Nothing else has to change — the decoder already matches against
    the table. `python3 src/dcs.py --check` reports how many codes frame uniquely,
    and should print 104/104 once the table is right.
"""

from __future__ import annotations

import itertools

# g(x) = x^11 + x^9 + x^7 + x^6 + x^5 + x + 1. Either this or its reciprocal
# generates the binary Golay code; they differ only in bit order convention.
GOLAY_POLY = 0xAE3

WORD_BITS = 23
DATA_BITS = 12
CODE_BITS = 9
FIXED_TRIPLE = 0b100        # information bits 9..11

BPS = 134.4                 # bit rate; the word therefore repeats at 5.84 Hz

# The standard three-digit octal codes in common use. Anything outside this list
# decodes arithmetically but is not a code any radio will let you dial in, so it is
# reported as a decode failure rather than as a surprising number. If a radio on
# site offers a code that is not here, this tuple is what to extend.
#
# This list is the only thing standing between "decoded" and "made up": Golay
# correction maps EVERY 23-bit input to some codeword, so a window of pure noise
# still yields a code. Noise clears the fixed triple 1 time in 8 and lands in this
# list 104 times in 512, so a single random word is accepted 1 time in 39. That is
# why the receiver requires the same code from repeated words rather than trusting
# one decode — see decode_stream() in survey_prototype.py.
STANDARD_CODES = (
    "023", "025", "026", "031", "032", "036", "043", "047", "051", "053",
    "054", "065", "071", "072", "073", "074", "114", "115", "116", "122",
    "125", "131", "132", "134", "143", "145", "152", "155", "156", "162",
    "165", "172", "174", "205", "212", "223", "225", "226", "243", "244",
    "245", "246", "251", "252", "255", "261", "263", "265", "266", "271",
    "274", "306", "311", "315", "325", "331", "332", "343", "346", "351",
    "356", "364", "365", "371", "411", "412", "413", "423", "431", "432",
    "445", "446", "452", "454", "455", "462", "464", "465", "466", "503",
    "506", "516", "523", "526", "532", "546", "565", "606", "612", "624",
    "627", "631", "632", "654", "662", "664", "703", "712", "723", "731",
    "732", "734", "743", "754",
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


def decode(word23: int, *, standard_only: bool = True,
           unique_only: bool = True) -> tuple[str, int] | None:
    """23-bit received word -> (octal code, bit errors), or None if it is not one.

    Rejects on three independent grounds: the fixed triple not surviving Golay
    correction, the code not being one a radio can be set to, and — unless
    `unique_only` is off — the code not being one whose framing is unambiguous.
    All three matter: Golay correction maps *every* input to some codeword, so
    without them a window of noise decodes to a plausible code 1 time in 39.
    """
    data, nerr = golay_decode(word23)
    if (data >> CODE_BITS) != FIXED_TRIPLE:
        return None
    code = format(data & ((1 << CODE_BITS) - 1), "03o")
    if standard_only and code not in STANDARD_CODES:
        return None
    if unique_only and code not in UNAMBIGUOUS_CODES:
        return None
    return code, nerr


# Every code whose framing is unambiguous under the current table. Codes outside
# this set are decodable only as "DCS present"; see the module docstring.
def _unique_framings() -> frozenset[str]:
    all1 = (1 << WORD_BITS) - 1
    out = []
    for code in STANDARD_CODES:
        word = encode(code)
        seen = set()
        for pol in (0, all1):
            v = word ^ pol
            for r in range(WORD_BITS):
                rotated = ((v >> r) | (v << (WORD_BITS - r))) & all1
                got = decode(rotated, unique_only=False)
                if got:
                    seen.add(got[0])
        if seen == {code}:
            out.append(code)
    return frozenset(out)


UNAMBIGUOUS_CODES = _unique_framings()


def bit_sequence(code: str, polarity: str = "N") -> list[int]:
    """The 23 bits as transmitted, LSB first. 'I' inverts, as an inverted DCS does."""
    word = encode(code)
    bits = [(word >> k) & 1 for k in range(WORD_BITS)]
    if polarity == "I":
        bits = [1 - b for b in bits]
    elif polarity != "N":
        raise ValueError(f"polarity must be 'N' or 'I', got {polarity!r}")
    return bits


def _check() -> int:
    """Report how unambiguous the current table is. 104/104 means it is right."""
    n = len(UNAMBIGUOUS_CODES)
    print(f"Golay syndrome table: {len(_SYNDROMES)} cosets (perfect code)")
    print(f"standard codes:       {len(STANDARD_CODES)}")
    print(f"unique framing:       {n}/{len(STANDARD_CODES)}")
    if n < len(STANDARD_CODES):
        print("\nThe codeword table is not the real one — see the module docstring.")
        print("Codes without a unique framing are reported as 'DCS present, code")
        print("unknown' rather than guessed at.")
    return 0 if n == len(STANDARD_CODES) else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_check())
