"""Tests for signal engine."""
import unittest, pandas as pd
from options_scanner.signals.engine import (evaluate,_pc_ratio_direction,_iv_skew_direction,
    _delta_weighted_pc_direction,_volume_anomaly_direction,_composite,SignalResult)
from options_scanner.signals.volume_history import VolumeHistory

def _df(cv=500.,pv=500.,ci=0.20,pi=0.20,cd=0.5,pd_=-0.5):
    return pd.DataFrame({'right':['C','P'],'volume':[cv,pv],'iv':[ci,pi],'delta':[cd,pd_],'strike':[100.,100.]})

def _hist(n=10,c=500.,p=500.):
    vh=VolumeHistory('T')
    for _ in range(n): vh.record(c,p)
    return vh

class TestPCRatio(unittest.TestCase):
    def test_high_bearish(self):   self.assertEqual(_pc_ratio_direction(100,200),'bearish')
    def test_low_bullish(self):    self.assertEqual(_pc_ratio_direction(200,100),'bullish')
    def test_neutral_none(self):   self.assertIsNone(_pc_ratio_direction(100,100))
    def test_zero_calls_bearish(self): self.assertEqual(_pc_ratio_direction(0,100),'bearish')
    def test_zero_both_none(self): self.assertIsNone(_pc_ratio_direction(0,0))
    def test_exact_bearish_threshold(self): self.assertEqual(_pc_ratio_direction(100,151),'bearish')   # 1.51 > 1.5
    def test_exact_bullish_threshold(self): self.assertEqual(_pc_ratio_direction(300,199),'bullish')   # 0.663 < 0.67

class TestIVSkew(unittest.TestCase):
    def test_high_put_bearish(self):  self.assertEqual(_iv_skew_direction(_df(ci=0.20,pi=0.25)),'bearish')
    def test_high_call_bullish(self): self.assertEqual(_iv_skew_direction(_df(ci=0.25,pi=0.20)),'bullish')
    def test_equal_none(self):        self.assertIsNone(_iv_skew_direction(_df(ci=0.20,pi=0.20)))
    def test_missing_iv_none(self):
        df=pd.DataFrame({'right':['C','P'],'iv':[None,None],'volume':[100,100],'delta':[.5,-.5]})
        self.assertIsNone(_iv_skew_direction(df))
    def test_only_calls_none(self):
        df=pd.DataFrame({'right':['C'],'iv':[0.20],'volume':[100],'delta':[.5]})
        self.assertIsNone(_iv_skew_direction(df))

class TestDeltaWeightedPC(unittest.TestCase):
    def test_heavy_put_bearish(self):  self.assertEqual(_delta_weighted_pc_direction(_df(cv=100,pv=300)),'bearish')
    def test_heavy_call_bullish(self): self.assertEqual(_delta_weighted_pc_direction(_df(cv=300,pv=100)),'bullish')
    def test_balanced_none(self):      self.assertIsNone(_delta_weighted_pc_direction(_df(cv=200,pv=200)))
    def test_no_delta_none(self):
        df=pd.DataFrame({'right':['C','P'],'volume':[100,200],'iv':[.2,.2],'delta':[None,None]})
        self.assertIsNone(_delta_weighted_pc_direction(df))

class TestVolumeAnomaly(unittest.TestCase):
    def test_put_spike_bearish(self):   self.assertEqual(_volume_anomaly_direction(500,1500,_hist()),'bearish')
    def test_call_spike_bullish(self):  self.assertEqual(_volume_anomaly_direction(1500,500,_hist()),'bullish')
    def test_both_spike_none(self):     self.assertIsNone(_volume_anomaly_direction(1500,1500,_hist()))
    def test_no_history_none(self):     self.assertIsNone(_volume_anomaly_direction(1000,2000,VolumeHistory('T')))
    def test_below_threshold_none(self):self.assertIsNone(_volume_anomaly_direction(500,900,_hist()))

class TestComposite(unittest.TestCase):
    def test_all_bearish_strong(self):
        self.assertEqual(_composite({k:'bearish' for k in 'abcd'}),'STRONG_BEARISH')
    def test_all_bullish_strong(self):
        self.assertEqual(_composite({k:'bullish' for k in 'abcd'}),'STRONG_BULLISH')
    def test_three_bearish_one_none(self):
        self.assertEqual(_composite({'a':'bearish','b':'bearish','c':'bearish','d':None}),'BEARISH')
    def test_three_bullish_one_none(self):
        self.assertEqual(_composite({'a':'bullish','b':'bullish','c':'bullish','d':None}),'BULLISH')
    def test_mixed_none(self):
        self.assertIsNone(_composite({'a':'bearish','b':'bullish','c':'bearish','d':None}))
    def test_all_none(self):
        self.assertIsNone(_composite({k:None for k in 'abcd'}))
    def test_two_agree_none(self):
        self.assertIsNone(_composite({'a':'bearish','b':'bearish','c':None,'d':None}))

class TestEvaluate(unittest.TestCase):
    def test_returns_signal_result(self):
        self.assertIsInstance(evaluate(_df(),_hist()),SignalResult)
    def test_all_bearish_fires(self):
        df=_df(cv=100,pv=300,ci=0.20,pi=0.26)
        vh=_hist(10,100.,100.); vh.record(100,300)
        r=evaluate(df,vh); self.assertIn(r.composite,('BEARISH','STRONG_BEARISH'))
    def test_pc_ratio_computed(self):
        r=evaluate(_df(cv=200.,pv=100.),_hist())
        self.assertIsNotNone(r.pc_ratio); self.assertAlmostEqual(r.pc_ratio,0.5)
    def test_zero_call_volume(self):
        r=evaluate(_df(cv=0.,pv=200.),_hist())
        self.assertEqual(r.call_vol,0.); self.assertIsNone(r.pc_ratio)
    def test_empty_df_graceful(self):
        df=pd.DataFrame(columns=['right','volume','iv','delta'])
        r=evaluate(df,VolumeHistory('T'))
        self.assertIsNone(r.composite); self.assertEqual(r.factors_active,0)

if __name__=='__main__': unittest.main()
