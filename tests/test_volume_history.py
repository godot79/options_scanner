"""Tests for VolumeHistory."""
import unittest
from options_scanner.signals.volume_history import VolumeHistory

class TestVolumeHistory(unittest.TestCase):
    def test_empty_avg(self):
        self.assertEqual(VolumeHistory('T').rolling_avg(),{'call':0.,'put':0.})
    def test_single_record_avg_zero(self):
        vh=VolumeHistory('T'); vh.record(100.,200.)
        self.assertEqual(vh.rolling_avg(),{'call':0.,'put':0.})
    def test_two_records_excludes_last(self):
        vh=VolumeHistory('T'); vh.record(100.,200.); vh.record(300.,400.)
        a=vh.rolling_avg()
        self.assertAlmostEqual(a['call'],100.); self.assertAlmostEqual(a['put'],200.)
    def test_rolling_avg_multiple(self):
        vh=VolumeHistory('T')
        for i in range(5): vh.record(float(i*100),float(i*50))
        a=vh.rolling_avg()
        self.assertAlmostEqual(a['call'],(0+100+200+300)/4,delta=1e-6)
        self.assertAlmostEqual(a['put'], (0+50+100+150)/4, delta=1e-6)
    def test_len(self):
        vh=VolumeHistory('T'); self.assertEqual(len(vh),0); vh.record(1.,1.); self.assertEqual(len(vh),1)
    def test_latest_none_empty(self):
        self.assertIsNone(VolumeHistory('T').latest())
    def test_latest_returns_last(self):
        vh=VolumeHistory('T'); vh.record(10.,20.)
        l=vh.latest(); self.assertEqual(l['call_vol'],10.); self.assertEqual(l['put_vol'],20.)
    def test_trim_respected(self):
        vh=VolumeHistory('T')
        for i in range(5000): vh.record(float(i),float(i))
        self.assertLessEqual(len(vh),vh._max_records())
    def test_serialise_round_trip(self):
        vh=VolumeHistory('T'); vh.record(100.,200.); vh.record(150.,250.)
        vh2=VolumeHistory.from_list('T',vh.to_list())
        self.assertEqual(len(vh2),2)
        a1=vh.rolling_avg(); a2=vh2.rolling_avg()
        self.assertAlmostEqual(a1['call'],a2['call']); self.assertAlmostEqual(a1['put'],a2['put'])
    def test_from_list_empty(self):
        vh=VolumeHistory.from_list('T',[])
        self.assertEqual(len(vh),0); self.assertEqual(vh.rolling_avg(),{'call':0.,'put':0.})
    def test_record_has_ts(self):
        vh=VolumeHistory('T'); vh.record(1.,2.); self.assertIn('ts',vh.latest())

if __name__=='__main__': unittest.main()
