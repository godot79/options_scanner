"""Tests for Black-76 model."""
import math
import unittest
from options_scanner.models.black76 import Black76Model

MODEL = Black76Model()
ATM   = dict(F=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20)
ITM   = dict(F=110.0, K=100.0, T=0.5, r=0.05, sigma=0.25)

class TestBlack76Price(unittest.TestCase):
    def test_atm_call_positive(self):
        self.assertGreater(MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C'),0)
    def test_atm_put_positive(self):
        self.assertGreater(MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P'),0)
    def test_zero_time_returns_zero(self):
        self.assertEqual(MODEL.price(110.0,100.0,0.0,0.05,0.20,'C'),0.0)
    def test_zero_sigma_returns_zero(self):
        self.assertEqual(MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],0.0,'C'),0.0)
    def test_put_call_parity_atm(self):
        c=MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        p=MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P')
        self.assertAlmostEqual(c-p, math.exp(-ATM['r']*ATM['T'])*(ATM['F']-ATM['K']), places=8)
    def test_put_call_parity_itm(self):
        c=MODEL.price(ITM['F'],ITM['K'],ITM['T'],ITM['r'],ITM['sigma'],'C')
        p=MODEL.price(ITM['F'],ITM['K'],ITM['T'],ITM['r'],ITM['sigma'],'P')
        self.assertAlmostEqual(c-p, math.exp(-ITM['r']*ITM['T'])*(ITM['F']-ITM['K']), places=8)
    def test_call_increases_with_sigma(self):
        lo=MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],0.10,'C')
        hi=MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],0.40,'C')
        self.assertGreater(hi,lo)
    def test_call_decreases_with_strike(self):
        self.assertGreater(MODEL.price(100,90,1,0.05,0.20,'C'),MODEL.price(100,110,1,0.05,0.20,'C'))
    def test_put_increases_with_strike(self):
        self.assertGreater(MODEL.price(100,110,1,0.05,0.20,'P'),MODEL.price(100,90,1,0.05,0.20,'P'))
    def test_right_case_insensitive(self):
        self.assertAlmostEqual(MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C'),
                               MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'c'),places=12)

class TestBlack76Greeks(unittest.TestCase):
    def test_call_delta_in_range(self):
        g=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertIsNotNone(g.delta); self.assertGreater(g.delta,0); self.assertLess(g.delta,1)
    def test_put_delta_negative(self):
        g=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P')
        self.assertIsNotNone(g.delta); self.assertLess(g.delta,0); self.assertGreater(g.delta,-1)
    def test_gamma_positive(self):
        g=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertIsNotNone(g.gamma); self.assertGreater(g.gamma,0)
    def test_vega_positive(self):
        g=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertIsNotNone(g.vega); self.assertGreater(g.vega,0)
    def test_call_put_same_gamma(self):
        gc=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        gp=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P')
        self.assertAlmostEqual(gc.gamma,gp.gamma,places=10)
    def test_call_put_same_vega(self):
        gc=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        gp=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P')
        self.assertAlmostEqual(gc.vega,gp.vega,places=10)
    def test_greeks_invalid_zero_underlying(self):
        self.assertFalse(MODEL.greeks(0.0,100.0,1.0,0.05,0.20,'C').is_valid())
    def test_delta_finite_diff_consistent(self):
        eps=0.01
        fd=(MODEL.price(ATM['F']+eps,ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')-
            MODEL.price(ATM['F']-eps,ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C'))/(2*eps)
        g=MODEL.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertAlmostEqual(g.delta,fd,delta=1e-4)

class TestBlack76IV(unittest.TestCase):
    def _rt(self,sigma,right='C'):
        price=MODEL.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],sigma,right)
        iv=MODEL.iv(ATM['F'],ATM['K'],ATM['T'],ATM['r'],price,right)
        self.assertIsNotNone(iv); self.assertAlmostEqual(iv,sigma,delta=1e-5)
    def test_rt_c_10(self): self._rt(0.10,'C')
    def test_rt_c_20(self): self._rt(0.20,'C')
    def test_rt_c_35(self): self._rt(0.35,'C')
    def test_rt_c_50(self): self._rt(0.50,'C')
    def test_rt_c_80(self): self._rt(0.80,'C')
    def test_rt_p_20(self): self._rt(0.20,'P')
    def test_rt_p_35(self): self._rt(0.35,'P')
    def test_iv_none_zero_price(self):
        self.assertIsNone(MODEL.iv(ATM['F'],ATM['K'],ATM['T'],ATM['r'],0.0,'C'))
    def test_iv_none_negative_price(self):
        self.assertIsNone(MODEL.iv(ATM['F'],ATM['K'],ATM['T'],ATM['r'],-1.0,'C'))
    def test_iv_none_zero_T(self):
        self.assertIsNone(MODEL.iv(ATM['F'],ATM['K'],0.0,ATM['r'],5.0,'C'))

if __name__=='__main__': unittest.main()
