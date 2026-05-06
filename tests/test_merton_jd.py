"""Tests for Merton Jump-Diffusion model."""
import math, unittest
from options_scanner.models.merton_jd import MertonJDModel
from options_scanner.models.black76 import Black76Model
M=MertonJDModel(); B76=Black76Model()
ATM=dict(F=100.,K=100.,T=1.,r=0.05,sigma=0.20)
P=dict(lam=0.1,mu_j=0.,sigma_j=0.1,is_futures=True,q=0.)

class TestMertonPrice(unittest.TestCase):
    def test_call_positive(self):
        self.assertGreater(M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P),0)
    def test_jump_adds_value_vs_b76(self):
        pm=M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P)
        pb=B76.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertGreaterEqual(pm,pb-1e-6)
    def test_zero_jump_converges_to_b76(self):
        p0=dict(lam=0.,mu_j=0.,sigma_j=0.1,is_futures=True,q=0.)
        pm=M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**p0)
        pb=B76.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertAlmostEqual(pm,pb,delta=1e-4)
    def test_put_call_parity(self):
        c=M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P)
        p=M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P',**P)
        parity=math.exp(-ATM['r']*ATM['T'])*(ATM['F']-ATM['K'])
        self.assertAlmostEqual(c-p,parity,delta=1e-3)
    def test_equity_mode(self):
        pe=dict(lam=0.1,mu_j=0.,sigma_j=0.1,is_futures=False,q=0.)
        self.assertGreater(M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**pe),0)
    def test_zero_sigma_guard(self):
        self.assertEqual(M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],0.,'C',**P),0.)

class TestMertonGreeks(unittest.TestCase):
    def test_call_delta_positive(self):
        g=M.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P)
        self.assertIsNotNone(g.delta); self.assertGreater(g.delta,0)
    def test_put_delta_negative(self):
        g=M.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P',**P)
        self.assertIsNotNone(g.delta); self.assertLess(g.delta,0)
    def test_gamma_positive(self):
        g=M.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P)
        self.assertIsNotNone(g.gamma); self.assertGreater(g.gamma,0)
    def test_vega_positive(self):
        g=M.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P)
        self.assertIsNotNone(g.vega); self.assertGreater(g.vega,0)
    def test_all_greeks_valid(self):
        self.assertTrue(M.greeks(ATM['F'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',**P).is_valid())
    def test_invalid_on_zero_underlying(self):
        self.assertFalse(M.greeks(0.,100.,1.,0.05,0.20,'C',**P).is_valid())

class TestMertonIV(unittest.TestCase):
    def test_iv_round_trip(self):
        sigma=0.25
        price=M.price(ATM['F'],ATM['K'],ATM['T'],ATM['r'],sigma,'C',**P)
        iv=M.iv(ATM['F'],ATM['K'],ATM['T'],ATM['r'],price,'C',**P)
        self.assertIsNotNone(iv); self.assertAlmostEqual(iv,sigma,delta=1e-3)
    def test_iv_none_zero_price(self):
        self.assertIsNone(M.iv(ATM['F'],ATM['K'],ATM['T'],ATM['r'],0.,'C',**P))
    def test_iv_none_zero_underlying(self):
        self.assertIsNone(M.iv(0.,ATM['K'],ATM['T'],ATM['r'],5.,'C',**P))

if __name__=='__main__': unittest.main()
