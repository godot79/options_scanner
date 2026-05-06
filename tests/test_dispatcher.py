"""Tests for model dispatcher."""
import unittest
from options_scanner.models.dispatcher import compute,get_model,REGISTRY
from options_scanner.models.base import PricingResult
FOP=dict(secType='FOP',model='black76',div_yield=0.,jd_lambda=0.1,jd_mu_j=0.,jd_sigma_j=0.1)
OPT=dict(secType='OPT',model='bsm',   div_yield=0.,jd_lambda=0.1,jd_mu_j=0.,jd_sigma_j=0.1)
JD =dict(secType='FOP',model='merton_jd',div_yield=0.,jd_lambda=0.1,jd_mu_j=0.,jd_sigma_j=0.1)

class TestGetModel(unittest.TestCase):
    def test_known_models(self):
        for n in ('black76','bsm','merton_jd'):
            m=get_model(n); self.assertEqual(m.NAME,n)
    def test_unknown_raises(self):
        with self.assertRaises(KeyError): get_model('heston')
    def test_registry_has_all(self):
        for n in ('black76','bsm','merton_jd'): self.assertIn(n,REGISTRY)

class TestDispatcherCompute(unittest.TestCase):
    def test_b76_valid(self):
        r=compute(FOP,100.,100.,1.,0.05,8.,'C')
        self.assertIsInstance(r,PricingResult); self.assertTrue(r.is_valid); self.assertEqual(r.model,'black76')
    def test_bsm_valid(self):
        r=compute(OPT,100.,100.,1.,0.05,10.,'C')
        self.assertIsInstance(r,PricingResult); self.assertTrue(r.is_valid); self.assertEqual(r.model,'bsm')
    def test_merton_valid(self):
        r=compute(JD,100.,100.,1.,0.05,9.,'C')
        self.assertIsInstance(r,PricingResult); self.assertEqual(r.model,'merton_jd')
    def test_null_on_bad_price(self):
        self.assertFalse(compute(FOP,100.,100.,1.,0.05,0.,'C').is_valid)
    def test_null_on_bad_underlying(self):
        self.assertFalse(compute(FOP,0.,100.,1.,0.05,5.,'C').is_valid)
    def test_call_delta_positive(self):
        r=compute(FOP,100.,100.,1.,0.05,8.,'C'); self.assertGreater(r.greeks.delta,0)
    def test_put_delta_negative(self):
        r=compute(FOP,100.,100.,1.,0.05,8.,'P'); self.assertLess(r.greeks.delta,0)
    def test_iv_positive(self):
        r=compute(OPT,150.,150.,0.5,0.05,15.,'C'); self.assertIsNotNone(r.iv); self.assertGreater(r.iv,0)

if __name__=='__main__': unittest.main()
