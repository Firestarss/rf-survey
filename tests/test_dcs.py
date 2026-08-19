"""Golay(23,12) and the DCS codeword layer.

The arithmetic here is the one part of DCS handling that is provably correct
rather than conventional, so it is tested hard. The framing convention is not
established — see the src/dcs.py docstring — and the tests reflect that: they
assert the decoder never reports a *wrong* code, not that it decodes everything.
"""

import random
import unittest

from support import SRC  # noqa: F401  (puts src on the path)

import dcs


class TestGolay(unittest.TestCase):

    def test_code_is_perfect(self):
        # sum(C(23,k) for k in 0..3) == 2048 == 2^11. If the generator polynomial
        # were wrong this would collide or come up short, and every correction
        # after it would be quietly ambiguous.
        self.assertEqual(len(dcs._SYNDROMES), 2048)
        self.assertEqual(len(set(dcs._SYNDROMES.values())), 2048)

    def test_encode_is_systematic(self):
        for data in (0x000, 0xFFF, 0x123, 0xABC):
            word = dcs.golay_encode(data)
            self.assertEqual(word >> 11, data, "information bits must survive")
            self.assertEqual(dcs._mod_g(word), 0, "codeword must have zero syndrome")

    def test_round_trip_every_standard_code(self):
        for code in dcs.STANDARD_CODES:
            got = dcs.decode(dcs.encode(code), unique_only=False)
            self.assertEqual(got, (code, 0), f"round trip failed for {code}")

    def test_corrects_up_to_three_bit_errors(self):
        rng = random.Random(20260819)
        for code in dcs.STANDARD_CODES:
            word = dcs.encode(code)
            for weight in (1, 2, 3):
                for _ in range(4):
                    err = 0
                    for bit in rng.sample(range(dcs.WORD_BITS), weight):
                        err |= 1 << bit
                    got = dcs.decode(word ^ err, unique_only=False)
                    self.assertEqual(
                        got, (code, weight),
                        f"{code} with {weight} flipped bits decoded as {got}")

    def test_four_bit_errors_never_return_the_original_code(self):
        # Minimum distance is 7, so a weight-4 error leaves the received word at
        # distance 4 from the true codeword and at distance >= 3 from every other
        # one. Syndrome decoding always picks a codeword within distance 3, so it
        # can never pick the true one back. This is exact, not statistical: if it
        # ever "succeeds" the arithmetic is wrong.
        rng = random.Random(7)
        for code in dcs.STANDARD_CODES[:30]:
            word = dcs.encode(code)
            for _ in range(8):
                err = 0
                for bit in rng.sample(range(dcs.WORD_BITS), 4):
                    err |= 1 << bit
                got = dcs.decode(word ^ err, unique_only=False)
                if got is not None:
                    self.assertNotEqual(got[0], code,
                                        "4-bit error was corrected to the true code")


class TestDcsFraming(unittest.TestCase):

    def test_fixed_triple_is_required(self):
        # A word that is a valid codeword but carries the wrong fixed triple must
        # be rejected. Without this check a window of noise is accepted 1 time in
        # 2048 rather than 1 in 16384.
        for triple in range(8):
            if triple == dcs.FIXED_TRIPLE:
                continue
            data = (triple << dcs.CODE_BITS) | int("023", 8)
            self.assertIsNone(dcs.decode(dcs.golay_encode(data), unique_only=False),
                              f"fixed triple {triple:03b} should not decode")

    def test_non_standard_codes_are_rejected(self):
        rejected = 0
        for value in range(1 << dcs.CODE_BITS):
            code = format(value, "03o")
            if code in dcs.STANDARD_CODES:
                continue
            data = (dcs.FIXED_TRIPLE << dcs.CODE_BITS) | value
            self.assertIsNone(dcs.decode(dcs.golay_encode(data), unique_only=False))
            rejected += 1
        self.assertGreater(rejected, 300, "test should be exercising many codes")

    def test_unique_only_filters_to_unambiguous_codes(self):
        for code in dcs.STANDARD_CODES:
            got = dcs.decode(dcs.encode(code), unique_only=True)
            if code in dcs.UNAMBIGUOUS_CODES:
                self.assertEqual(got, (code, 0))
            else:
                self.assertIsNone(got, f"{code} is ambiguous and must not decode")

    def test_never_reports_a_wrong_code_under_any_framing(self):
        """The safety property. This is the one that must never regress.

        A DCS stream has no sync pattern, so a receiver must try all 23 rotations
        in both polarities. The code is cyclic and all-ones is a codeword, so
        every one of those 46 framings is a valid codeword. Whatever comes back
        must be either the transmitted code or nothing — never a different one.
        """
        all_ones = (1 << dcs.WORD_BITS) - 1
        for code in dcs.STANDARD_CODES:
            word = dcs.encode(code)
            for polarity in (0, all_ones):
                value = word ^ polarity
                for rot in range(dcs.WORD_BITS):
                    rotated = ((value >> rot)
                               | (value << (dcs.WORD_BITS - rot))) & all_ones
                    got = dcs.decode(rotated, unique_only=True)
                    if got is not None:
                        self.assertEqual(
                            got[0], code,
                            f"transmitting {code} decoded as {got[0]} "
                            f"at rotation {rot}")

    def test_ambiguity_is_reported_honestly(self):
        # If this ever reaches 104/104 the codeword table has been corrected and
        # the caveats in the docstrings should come out. Until then the count is
        # a fact worth pinning so nobody assumes DCS is finished.
        self.assertLess(len(dcs.UNAMBIGUOUS_CODES), len(dcs.STANDARD_CODES),
                        "table now unambiguous — update src/dcs.py docs and "
                        "remove the never-guess caveats")
        self.assertGreaterEqual(len(dcs.UNAMBIGUOUS_CODES), 43)

    def test_bit_sequence_polarity_is_exact_complement(self):
        for code in list(dcs.STANDARD_CODES)[:12]:
            n = dcs.bit_sequence(code, "N")
            i = dcs.bit_sequence(code, "I")
            self.assertEqual(len(n), dcs.WORD_BITS)
            self.assertEqual(i, [1 - b for b in n])
        with self.assertRaises(ValueError):
            dcs.bit_sequence("023", "X")

    def test_random_words_are_rarely_accepted(self):
        # Golay maps every input to some codeword, so acceptance is governed by
        # the fixed triple (1/8) and the unambiguous list (43/512): about 1.05%.
        # Seeded, so this is deterministic rather than flaky.
        rng = random.Random(99)
        n = 20000
        hits = sum(1 for _ in range(n)
                   if dcs.decode(rng.getrandbits(23)) is not None)
        self.assertLess(hits / n, 0.03, "far too many random words accepted")
        self.assertGreater(hits, 0, "test is not exercising the accept path")


if __name__ == "__main__":
    unittest.main()
