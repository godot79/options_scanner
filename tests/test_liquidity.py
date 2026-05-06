"""Tests for liquidity metrics."""
import math, unittest
import pandas as pd
from options_scanner.data.liquidity import compute_liquidity_metrics

def _df(**kw):
    d=dict(bid=[1.,2.,0.5],ask=[1.2,2.4,0.8],volume=[100,500,50],openInterest=[1000,5000,200])
    d.update(kw); return pd.DataFrame(d)

class TestLiquidity(unittest.TestCase):
    def test_returns_df(self):
        self.assertIsInstance(compute_liquidity_metrics(_df()),pd.DataFrame)
    def test_no_mutation(self):
        df=_df(); orig=df.copy(); compute_liquidity_metrics(df); pd.testing.assert_frame_equal(df,orig)
    def test_cols_present(self):
        out=compute_liquidity_metrics(_df())
        for c in ('mid','spread','spread_pct','vol_score','oi_score','spread_score','liquidity_score'):
            self.assertIn(c,out.columns)
    def test_score_unit_interval(self):
        out=compute_liquidity_metrics(_df())
        self.assertTrue(out['liquidity_score'].between(0.,1.).all())
    def test_mid_correct(self):
        out=compute_liquidity_metrics(pd.DataFrame(dict(bid=[1.],ask=[1.4],volume=[10],openInterest=[100])))
        self.assertAlmostEqual(out['mid'].iloc[0],1.2)
    def test_spread_correct(self):
        out=compute_liquidity_metrics(pd.DataFrame(dict(bid=[1.],ask=[1.4],volume=[10],openInterest=[100])))
        self.assertAlmostEqual(out['spread'].iloc[0],0.4)
    def test_tight_spread_scores_higher(self):
        tight=compute_liquidity_metrics(pd.DataFrame(dict(bid=[9.9],ask=[10.1],volume=[100],openInterest=[1000])))
        wide =compute_liquidity_metrics(pd.DataFrame(dict(bid=[9.],ask=[11.],volume=[100],openInterest=[1000])))
        self.assertGreater(tight['liquidity_score'].iloc[0],wide['liquidity_score'].iloc[0])
    def test_higher_volume_scores_higher(self):
        # Both rows in same df so min-max normalisation differentiates them
        df = pd.DataFrame(dict(bid=[1.,1.],ask=[1.2,1.2],volume=[10,10000],openInterest=[100,100]))
        out = compute_liquidity_metrics(df)
        self.assertGreater(out['liquidity_score'].iloc[1], out['liquidity_score'].iloc[0])
    def test_none_bid_handled(self):
        df=pd.DataFrame(dict(bid=[None,1.],ask=[1.5,1.2],volume=[100,200],openInterest=[500,1000]))
        out=compute_liquidity_metrics(df)
        self.assertFalse(out['liquidity_score'].isna().any())
    def test_both_zero_score_zero(self):
        out=compute_liquidity_metrics(pd.DataFrame(dict(bid=[0.],ask=[0.],volume=[0],openInterest=[0])))
        self.assertEqual(out['liquidity_score'].iloc[0],0.)
    def test_nan_volume_no_crash(self):
        out=compute_liquidity_metrics(pd.DataFrame(dict(bid=[1.],ask=[1.2],volume=[float('nan')],openInterest=[float('nan')])))
        self.assertFalse(math.isnan(out['liquidity_score'].iloc[0]))
    def test_ranking(self):
        df=pd.DataFrame(dict(bid=[9.9,5.,1.],ask=[10.1,6.,2.],volume=[5000,500,50],openInterest=[10000,1000,100]))
        out=compute_liquidity_metrics(df)
        self.assertEqual(out['liquidity_score'].idxmax(),0)
    def test_zero_spread_score_one(self):
        out=compute_liquidity_metrics(pd.DataFrame(dict(bid=[10.],ask=[10.],volume=[100],openInterest=[1000])))
        self.assertAlmostEqual(out['spread_score'].iloc[0],1.0)
    def test_single_row(self):
        out=compute_liquidity_metrics(pd.DataFrame(dict(bid=[5.],ask=[5.5],volume=[10],openInterest=[100])))
        self.assertEqual(len(out),1); self.assertGreaterEqual(out['liquidity_score'].iloc[0],0.)

if __name__=='__main__': unittest.main()
