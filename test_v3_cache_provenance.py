import unittest
from v3_release_host import cached_quotes


class CacheProvenanceTests(unittest.TestCase):
    def get(self, row, **metadata):
        class Market:
            def read(self,codes):
                return dict(complete=True,quotes={'2330':row},**metadata),200
        return cached_quotes(Market(),['2330'])

    def test_synthetic_mode_rejected_even_if_row_label_is_live(self):
        with self.assertRaisesRegex(ValueError,'synthetic'):
            self.get(dict(source='Fugle shared snapshot',status='fresh_intraday'),data_mode='synthetic_only')

    def test_missing_or_wrong_source_rejected(self):
        for row in ({'price':100},dict(source='synthetic_test_fixture',status='synthetic_fixture')):
            with self.assertRaisesRegex(ValueError,'unverified'):self.get(row)

    def test_wrong_identity_or_status_rejected(self):
        for row in (dict(code='2382',source='Fugle shared snapshot',status='fresh_intraday'),dict(source='Fugle shared snapshot',status='stale')):
            with self.assertRaisesRegex(ValueError,'unverified'):self.get(row)

    def test_valid_source_preserves_row(self):
        row=dict(price=100,source='Fugle shared snapshot',status='final_close')
        self.assertEqual(self.get(row),{'2330':dict(row,code='2330')})

if __name__=='__main__':unittest.main()
