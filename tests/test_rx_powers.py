"""Legacy eye powers must still read as prescriptions.

A power stored as sphere only — ``-2.00`` — read as no prescription at all,
because every reader gates the prescription block on a ``/`` being present. The
decision is to tolerate the short form rather than rewrite historical rows.
"""
import unittest

from rx_powers import normalize_power, normalize_rows


class NormalizePower(unittest.TestCase):

    def test_four_part_power_is_untouched(self):
        self.assertEqual(normalize_power('-2.00/-0.50/90/2.00'),
                         '-2.00/-0.50/90/2.00')

    def test_sphere_only_gains_zero_components(self):
        """rx_id 8173 of LSEUKQ-999700: typed as sphere only."""
        self.assertEqual(normalize_power('-2.00'), '-2.00/0/0/0')

    def test_partial_power_is_completed(self):
        self.assertEqual(normalize_power('-2.00/-0.50'), '-2.00/-0.50/0/0')

    def test_blank_components_become_zero(self):
        self.assertEqual(normalize_power('-2.00//90/'), '-2.00/0/90/0')

    def test_whitespace_is_trimmed(self):
        self.assertEqual(normalize_power(' -2.00 / -0.50 '), '-2.00/-0.50/0/0')

    def test_extra_components_are_dropped(self):
        self.assertEqual(normalize_power('1/2/3/4/5'), '1/2/3/4')

    def test_non_prescriptions_stay_empty(self):
        for value in (None, '', '   ', '0', 'None', 'NULL', 'null', '-',
                      'No RX selected', 'no rx selected'):
            self.assertEqual(normalize_power(value), '',
                             msg='%r must not read as a prescription' % value)

    def test_plano_sphere_written_in_full_is_a_prescription(self):
        """'0' alone is a placeholder; '0/0/0/0' was written by checkout."""
        self.assertEqual(normalize_power('0/0/0/0'), '0/0/0/0')

    def test_contact_lens_colour_component_survives(self):
        self.assertEqual(normalize_power('-1.00/8.6/14.2/Grey'),
                         '-1.00/8.6/14.2/Grey')


class NormalizeRows(unittest.TestCase):

    def test_both_eyes_are_normalized_in_place(self):
        rows = [{'right_eye': '-2.00', 'left_eye': '-1.75'}]
        self.assertIs(normalize_rows(rows), rows)
        self.assertEqual(rows[0], {'right_eye': '-2.00/0/0/0',
                                   'left_eye': '-1.75/0/0/0'})

    def test_one_blind_eye_does_not_invent_a_power(self):
        rows = [{'right_eye': '-2.00', 'left_eye': None}]
        normalize_rows(rows)
        self.assertEqual(rows[0]['left_eye'], '')

    def test_rows_without_eyes_are_left_alone(self):
        rows = [{'order_id': 'LSEUKQ-999700'}]
        normalize_rows(rows)
        self.assertEqual(rows[0], {'order_id': 'LSEUKQ-999700'})

    def test_no_rows_is_not_an_error(self):
        self.assertIsNone(normalize_rows(None))

    def test_split_on_slash_finds_four_components_after_normalizing(self):
        """The shape every template depends on."""
        parts = normalize_power('-2.00').split('/')
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], '-2.00')


if __name__ == '__main__':
    unittest.main()
