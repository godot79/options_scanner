"""Tests for all fingerprint models and FingerprintEngine."""
import unittest, pandas as pd
from options_scanner.signals.fingerprint.base import Finding
from options_scanner.signals.fingerprint.oi_build import OIBuildModel, _longest_monotonic_suffix
from options_scanner.signals.fingerprint.volume_cluster import VolumeClusterModel
from options_scanner.signals.fingerprint.sweep_detector import SweepDetectorModel, _are_adjacent, _sweep_confidence
from options_scanner.signals.fingerprint.expiry_concentration import ExpiryConcentrationModel
from options_scanner.signals.fingerprint.engine import FingerprintEngine, _replace_confidence
from options_scanner.config import FINGERPRINT_CONFIG

I='SI'

def _row(exp='20250620',K=30.,r='C',oi=100,vol=200):
    return pd.DataFrame({'expiry':[exp],'strike':[float(K)],'right':[r],'openInterest':[float(oi)],'volume':[float(vol)]})

def _multi(n=5,exp='20250620',r='C',K0=28.,step=1.,vol=100,oi=500):
    return pd.DataFrame({'expiry':[exp]*n,'strike':[K0+i*step for i in range(n)],
        'right':[r]*n,'openInterest':[float(oi)]*n,'volume':[float(vol)]*n})

# ── OI Build ──────────────────────────────────────────────────────────────────
class TestOIBuild(unittest.TestCase):
    def _min(self): return FINGERPRINT_CONFIG['oi_build_min_scans']
    def test_no_findings_insufficient_history(self):
        m=OIBuildModel()
        for oi in [100,110]: m.update(I,_row(oi=oi))
        self.assertEqual(m.detect(I),[])
    def test_finds_monotonic_growth(self):
        m=OIBuildModel(); s=self._min()
        for oi in range(100,100+(s+1)*10,10): m.update(I,_row(oi=oi))
        f=m.detect(I); self.assertGreater(len(f),0); self.assertEqual(f[0].finding_type,'oi_build')
    def test_no_finding_flat(self):
        m=OIBuildModel()
        for _ in range(6): m.update(I,_row(oi=500))
        self.assertEqual(m.detect(I),[])
    def test_no_finding_decreasing(self):
        m=OIBuildModel()
        for oi in [500,490,480,470,460]: m.update(I,_row(oi=oi))
        self.assertEqual(m.detect(I),[])
    def test_clear_resets(self):
        m=OIBuildModel(); s=self._min()
        for oi in range(100,100+(s+1)*10,10): m.update(I,_row(oi=oi))
        m.clear(I); self.assertEqual(m.detect(I),[])
    def test_finding_fields(self):
        m=OIBuildModel(); s=self._min()
        for oi in range(100,100+(s+1)*10,10): m.update(I,_row(exp='20250620',K=30.,r='C',oi=oi))
        f=m.detect(I)
        if f: self.assertEqual(f[0].expiry,'20250620'); self.assertEqual(f[0].strike,30.); self.assertEqual(f[0].source,'ibkr_snapshot')

class TestMonotonicSuffix(unittest.TestCase):
    def test_all_increasing(self):   self.assertEqual(_longest_monotonic_suffix([1,2,3,4,5]),5)
    def test_last_three(self):       self.assertEqual(_longest_monotonic_suffix([5,3,2,3,4]),3)
    def test_flat(self):             self.assertEqual(_longest_monotonic_suffix([5,5,5]),0)
    def test_single(self):           self.assertEqual(_longest_monotonic_suffix([7]),0)
    def test_decreasing(self):       self.assertEqual(_longest_monotonic_suffix([5,4,3]),0)

# ── Volume Cluster ────────────────────────────────────────────────────────────
class TestVolumeCluster(unittest.TestCase):
    def _min(self): return FINGERPRINT_CONFIG['volume_cluster_min_strikes']
    def test_below_min_no_finding(self):
        m=VolumeClusterModel(); m.update(I,_multi(n=2,vol=100)); self.assertEqual(m.detect(I),[])
    def test_finds_cluster(self):
        m=VolumeClusterModel(); s=self._min(); m.update(I,_multi(n=s+1,vol=100))
        f=m.detect(I); self.assertGreater(len(f),0); self.assertEqual(f[0].finding_type,'volume_cluster')
    def test_confidence_in_range(self):
        m=VolumeClusterModel(); s=self._min(); m.update(I,_multi(n=s+2,vol=100))
        for f in m.detect(I): self.assertGreaterEqual(f.confidence,0.); self.assertLessEqual(f.confidence,1.)
    def test_clear(self):
        m=VolumeClusterModel(); s=self._min(); m.update(I,_multi(n=s+1,vol=100))
        m.clear(I); self.assertEqual(m.detect(I),[])

# ── Sweep Detector ────────────────────────────────────────────────────────────
class TestSweepDetector(unittest.TestCase):
    def _min(self): return FINGERPRINT_CONFIG['sweep_min_strikes']
    def _two_scan(self,n=5,v1=50,v2=150):
        m=SweepDetectorModel(); m.update(I,_multi(n=n,vol=v1)); m.update(I,_multi(n=n,vol=v2)); return m.detect(I)
    def test_detects_sweep(self):
        s=self._min(); f=self._two_scan(n=s+1)
        self.assertTrue(any(x.finding_type=='sweep' for x in f))
    def test_no_sweep_insufficient_strikes(self):
        s=self._min(); f=self._two_scan(n=max(1,s-1))
        self.assertFalse(any(x.finding_type=='sweep' for x in f))
    def test_no_sweep_unchanged_volume(self):
        m=SweepDetectorModel(); df=_multi(n=6,vol=100)
        m.update(I,df); m.update(I,df); f=m.detect(I)
        self.assertFalse(any(x.finding_type=='sweep' for x in f))
    def test_clear_resets(self):
        s=self._min(); m=SweepDetectorModel()
        m.update(I,_multi(n=s+1,vol=50)); m.update(I,_multi(n=s+1,vol=150)); m.clear(I)
        m.update(I,_multi(n=s+1,vol=200)); self.assertEqual(m.detect(I),[])

class TestAreAdjacent(unittest.TestCase):
    def test_uniform(self):   self.assertTrue(_are_adjacent([100.,101.,102.,103.]))
    def test_gap(self):       self.assertFalse(_are_adjacent([100.,101.,105.,106.]))
    def test_single(self):    self.assertTrue(_are_adjacent([100.]))
    def test_two(self):       self.assertTrue(_are_adjacent([100.,101.]))

class TestSweepConfidence(unittest.TestCase):
    def test_more_strikes_higher(self):
        c3=_sweep_confidence(3,3,[100.]*3,300.); c6=_sweep_confidence(6,3,[100.]*6,600.)
        self.assertGreater(c6,c3)
    def test_consistent_size_higher(self):
        cc=_sweep_confidence(4,3,[100.]*4,400.); cv=_sweep_confidence(4,3,[10.,500.,1.,300.],811.)
        self.assertGreater(cc,cv)
    def test_max_one(self):
        self.assertLessEqual(_sweep_confidence(20,3,[100.]*20,2000.),1.0)

# ── Expiry Concentration ──────────────────────────────────────────────────────
class TestExpiryConcentration(unittest.TestCase):
    def test_finds_concentration(self):
        m=ExpiryConcentrationModel()
        df=pd.DataFrame({'expiry':['20250620','20250620','20250720'],'strike':[30.,31.,30.],'right':['C','C','C'],
            'openInterest':[100.]*3,'volume':[450.,450.,100.]})
        m.update(I,df); f=m.detect(I); self.assertGreater(len(f),0); self.assertEqual(f[0].finding_type,'expiry_concentration')
    def test_no_finding_even_split(self):
        m=ExpiryConcentrationModel()
        df=pd.DataFrame({'expiry':['20250620','20250720'],'strike':[30.,30.],'right':['C','C'],
            'openInterest':[100.,100.],'volume':[500.,500.]})
        m.update(I,df); self.assertEqual(m.detect(I),[])
    def test_zero_volume_no_crash(self):
        m=ExpiryConcentrationModel(); m.update(I,_row(vol=0)); self.assertEqual(m.detect(I),[])
    def test_confidence_in_range(self):
        m=ExpiryConcentrationModel()
        df=pd.DataFrame({'expiry':['20250620','20250720'],'strike':[30.,30.],'right':['C','C'],
            'openInterest':[100.,100.],'volume':[800.,200.]})
        m.update(I,df)
        for f in m.detect(I): self.assertGreaterEqual(f.confidence,0.); self.assertLessEqual(f.confidence,1.)

# ── Engine ────────────────────────────────────────────────────────────────────
class TestFingerprintEngine(unittest.TestCase):
    def test_runs_without_error(self):
        e=FingerprintEngine(); e.update(I,_multi(n=5)); self.assertIsInstance(e.detect(I),list)
    def test_below_threshold_filtered(self):
        class LowConf:
            NAME='low'; accepts_source=['ibkr_snapshot']
            def update(self,inst,data): pass
            def clear(self,inst): pass
            def detect(self,inst):
                return [Finding(confidence=0.01,source='ibkr_snapshot',instrument=inst,model=self.NAME,finding_type='t',note='n')]
        e=FingerprintEngine(models=[LowConf()]); e.update(I,_row()); self.assertEqual(e.detect(I),[])
    def test_corroboration_bonus(self):
        class A:
            NAME='a'; accepts_source=['ibkr_snapshot']
            def update(self,inst,data): pass
            def clear(self,inst): pass
            def detect(self,inst):
                return [Finding(confidence=0.55,source='ibkr_snapshot',instrument=inst,model=self.NAME,
                    finding_type='t',note='n',expiry='20250620',strike=30.,right='C')]
        class B(A):
            NAME='b'
        e=FingerprintEngine(models=[A(),B()]); e.update(I,_row())
        f=e.detect(I); self.assertTrue(any(x.confidence>0.55 for x in f))
    def test_replace_confidence(self):
        f=Finding(confidence=0.5,source='ibkr_snapshot',instrument='SI',model='t',finding_type='t',note='n')
        f2=_replace_confidence(f,0.8)
        self.assertEqual(f2.confidence,0.8); self.assertEqual(f.confidence,0.5)

# ── Finding validation ────────────────────────────────────────────────────────
class TestFinding(unittest.TestCase):
    def test_valid(self):
        f=Finding(confidence=0.7,source='ibkr_snapshot',instrument='CL',model='m',finding_type='t',note='n')
        self.assertEqual(f.confidence,0.7)
    def test_bad_confidence(self):
        with self.assertRaises(ValueError):
            Finding(confidence=1.5,source='ibkr_snapshot',instrument='CL',model='m',finding_type='t',note='n')
    def test_bad_source(self):
        with self.assertRaises(ValueError):
            Finding(confidence=0.7,source='bad',instrument='CL',model='m',finding_type='t',note='n')
    def test_frozen(self):
        f=Finding(confidence=0.7,source='ibkr_snapshot',instrument='CL',model='m',finding_type='t',note='n')
        with self.assertRaises(Exception): f.confidence=0.9

if __name__=='__main__': unittest.main()
