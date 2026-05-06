"""Tests for BSM model."""
import math, unittest
from options_scanner.models.bsm import BSMModel
M=BSMModel()
ATM=dict(S=100.,K=100.,T=1.,r=0.05,sigma=0.20,q=0.)
DIV=dict(S=100.,K=100.,T=1.,r=0.05,sigma=0.20,q=0.02)

class TestBSMPrice(unittest.TestCase):
    def test_call_positive(self):
        self.assertGreater(M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.),0)
    def test_put_positive(self):
        self.assertGreater(M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P',q=0.),0)
    def test_pcp_no_dividend(self):
        c=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.)
        p=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P',q=0.)
        parity=ATM['S']-ATM['K']*math.exp(-ATM['r']*ATM['T'])
        self.assertAlmostEqual(c-p,parity,places=8)
    def test_pcp_with_dividend(self):
        c=M.price(DIV['S'],DIV['K'],DIV['T'],DIV['r'],DIV['sigma'],'C',q=DIV['q'])
        p=M.price(DIV['S'],DIV['K'],DIV['T'],DIV['r'],DIV['sigma'],'P',q=DIV['q'])
        parity=DIV['S']*math.exp(-DIV['q']*DIV['T'])-DIV['K']*math.exp(-DIV['r']*DIV['T'])
        self.assertAlmostEqual(c-p,parity,places=8)
    def test_dividend_reduces_call(self):
        c0=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.0)
        cd=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.05)
        self.assertGreater(c0,cd)
    def test_zero_sigma_guard(self):
        self.assertEqual(M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],0.,'C'),0.)
    def test_zero_T_guard(self):
        self.assertEqual(M.price(ATM['S'],ATM['K'],0.,ATM['r'],ATM['sigma'],'C'),0.)

class TestBSMGreeks(unittest.TestCase):
    def test_call_delta_in_range(self):
        g=M.greeks(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.)
        self.assertIsNotNone(g.delta); self.assertGreater(g.delta,0); self.assertLess(g.delta,1)
    def test_put_delta_negative(self):
        g=M.greeks(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P',q=0.)
        self.assertIsNotNone(g.delta); self.assertLess(g.delta,0); self.assertGreater(g.delta,-1)
    def test_call_put_delta_sum_nodiv(self):
        gc=M.greeks(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.)
        gp=M.greeks(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'P',q=0.)
        self.assertAlmostEqual(gc.delta+abs(gp.delta),1.0,places=6)
    def test_gamma_positive(self):
        g=M.greeks(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C')
        self.assertIsNotNone(g.gamma); self.assertGreater(g.gamma,0)
    def test_vega_matches_fd(self):
        eps=1e-4
        pu=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma']+eps,'C',q=0.)
        pd=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma']-eps,'C',q=0.)
        fd=(pu-pd)/(2*eps)
        g=M.greeks(ATM['S'],ATM['K'],ATM['T'],ATM['r'],ATM['sigma'],'C',q=0.)
        self.assertAlmostEqual(g.vega,fd,delta=1e-4)
    def test_greeks_invalid_zero_spot(self):
        self.assertFalse(M.greeks(0.,100.,1.,0.05,0.20,'C').is_valid())

class TestBSMIV(unittest.TestCase):
    def _rt(self,sigma,q=0.,right='C'):
        price=M.price(ATM['S'],ATM['K'],ATM['T'],ATM['r'],sigma,right,q=q)
        iv=M.iv(ATM['S'],ATM['K'],ATM['T'],ATM['r'],price,right,q=q)
        self.assertIsNotNone(iv); self.assertAlmostEqual(iv,sigma,delta=1e-5)
    def test_rt_c_10(self): self._rt(0.10)
    def test_rt_c_20(self): self._rt(0.20)
    def test_rt_c_35(self): self._rt(0.35)
    def test_rt_c_60(self): self._rt(0.60)
    def test_rt_div_20(self): self._rt(0.20,q=0.02)
    def test_rt_div_40(self): self._rt(0.40,q=0.02)
    def test_iv_none_bad_inputs(self):
        self.assertIsNone(M.iv(ATM['S'],ATM['K'],ATM['T'],ATM['r'],0.,'C'))
        self.assertIsNone(M.iv(ATM['S'],ATM['K'],0.,ATM['r'],5.,'C'))
        self.assertIsNone(M.iv(0.,ATM['K'],ATM['T'],ATM['r'],5.,'C'))

if __name__=='__main__': unittest.main()
