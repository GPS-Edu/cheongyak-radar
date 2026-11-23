import json
import tempfile
import unittest
from datetime import datetime,timezone,date
from pathlib import Path
from unittest.mock import patch
from market_v49 import analyse
from market_v52 import summarize
from validate_v52 import validate
from notice_events import update_events
from market_discovery import analyze as broad_analyze

NOW=date(2026,11,23)

def project(model='084.0000A', price=18.56):
    return {'id':'unit-test-1','name':'테스트 래미안 단지', 'region':'서울',
            'start':'2026-11-24','end':'2026-11-26','housingTypes':[{'type':model,'exclusiveAreaSqm':84,'priceEok':price}]}

def trade(name,price,area=84,year=2023,dt='2026-10-10'):
    return {'apartment':name,'dealAmountManwon':round(price*10000),
            'exclusiveAreaSqm':area,'dealDate':dt,'buildYear':str(year),
            'legalDong':'아무동','jibun':'12-1'}

class TestMarket(unittest.TestCase):
 def test_new_price_beats_old_stock(self):
    p=project(); rows=[trade('오래된 단지',12,year=1995) for _ in range(8)]+[trade('신축 가',39),trade('신축 나',41)]
    a=analyse(p,rows,[],NOW); summary=summarize(p,a,NOW)
    self.assertEqual(summary['baseline']['cohort'],'new_0_5')
    self.assertGreater(summary['baseline']['referenceEok'],35)
 def test_not_cross_area(self):
    p=project(); a=analyse(p,[trade('40억 다른 평형',40,area=59)],[],NOW)
    self.assertIsNone(summarize(p,a,NOW)['baseline'])
 def test_rights_separate_and_high_upside(self):
    p=project();a=analyse(p,[trade('준신축 한동',24,year=2018)],[],NOW)
    a['byType'][0]['extraRights']=[{'kind':'보도 입주권','referenceEok':43.2,'date':'2026-06-30'}]
    r=summarize(p,a,NOW)
    self.assertEqual(r['baseline']['category'],'apartment')
    self.assertEqual(r['headline']['category'],'rights')
    self.assertAlmostEqual(r['headline']['gapEok'],24.64)
 def test_no_comps_keeps_candidate_data(self):
    p=project();r=summarize(p,analyse(p,[],[],NOW),NOW)
    self.assertEqual(r['status'],'no_apartment_comparison')
    self.assertIsNone(r['baseline'])
 def test_weak_dong_not_needed(self):
    p=project();r=summarize(p,analyse(p,[trade('다른 법정동 인근 신축',34)],[],NOW),NOW)
    self.assertIsNotNone(r['baseline'])
 def test_bad_source_price_rejected(self):
    p=project();a=analyse(p,[trade('신축',32)],[],NOW)
    a['byType'][0]['saleEok']=19.00
    self.assertIsNone(summarize(p,a,NOW)['baseline'])
 def test_old_trade_excluded(self):
    p=project();r=summarize(p,analyse(p,[trade('오래된 거래',40,dt='2024-01-01')],[],NOW),NOW)
    self.assertIsNone(r['baseline'])
 def test_missing_buildyear_kept(self):
    p=project();a=analyse(p,[trade('연식 없음',25,year='')],[],NOW)
    self.assertEqual(summarize(p,a,NOW)['baseline']['cohort'],'age_unknown')
 def test_price_negative_still_visible(self):
    p=project();a=analyse(p,[trade('저가 아파트',12)],[],NOW)
    r=summarize(p,a,NOW)
    self.assertLess(r['baseline']['gapEok'],0)
 def test_no_false_name_verified(self):
    p=project();a=analyse(p,[trade('테스트 래미안 단지',40)],[],NOW)
    r=summarize(p,a,NOW)
    self.assertFalse(r['baseline']['sameComplexVerified'])
 def test_broad_signal_preserved(self):
    p=project();r=broad_analyze(p,[trade('완전히 다른 동',30)],NOW)
    self.assertTrue(r['candidate'])

class TestValidation(unittest.TestCase):
 def make_payload(self):
    p=project();m=analyse(p,[trade('테스트 근처 신축',32)],[],NOW)
    p['marketV49']=m;p['priceSummaryV52']=summarize(p,m,NOW)
    return {'updated_at':datetime.now(timezone.utc).isoformat(), 'projects':[p]}
 def test_publish_and_version(self):
    with tempfile.TemporaryDirectory() as d:
      src,target=Path(d)/'data.preview.json',Path(d)/'data.json'
      src.write_text(json.dumps(self.make_payload()))
      payload=validate(str(src),str(target))
      self.assertEqual(json.loads(target.read_text())['dataSchemaVersion'],52)
      self.assertEqual(payload['analysisQuality']['v52PriceCardNotices'],1)
 def test_reject_mismatched_price_preserve_last_good(self):
    with tempfile.TemporaryDirectory() as d:
      src,target=Path(d)/'source.json',Path(d)/'data.json'
      payload=self.make_payload();payload['projects'][0]['priceSummaryV52']['baseline']['saleEok']=200
      src.write_text(json.dumps(payload));target.write_text('OLD')
      with self.assertRaises(RuntimeError):validate(str(src),str(target))
      self.assertEqual(target.read_text(),'OLD')
 def test_reject_missing_card(self):
    with tempfile.TemporaryDirectory() as d:
      src,target=Path(d)/'source.json',Path(d)/'data.json'
      payload=self.make_payload();del payload['projects'][0]['priceSummaryV52']
      src.write_text(json.dumps(payload));target.write_text('OLD')
      with self.assertRaises(RuntimeError):validate(str(src),str(target))
      self.assertEqual(target.read_text(),'OLD')
 def test_reject_duplicate(self):
    with tempfile.TemporaryDirectory() as d:
      src=Path(d)/'source.json';payload=self.make_payload();payload['projects'].append(payload['projects'][0])
      src.write_text(json.dumps(payload))
      with self.assertRaisesRegex(RuntimeError,'중복'):validate(str(src),str(Path(d)/'out.json'))
 def test_notice_correction_detection(self):
    with tempfile.TemporaryDirectory() as d:
      existing=project();old=Path(d)/'data.json';old.write_text(json.dumps({'projects':[existing]}))
      changed={**existing,'end':'2026-11-27'}
      ev=update_events([changed],str(old),'2026-11-23')
      self.assertEqual(len(ev),1)
      self.assertEqual(ev[0]['kind'],'changed')

if __name__=='__main__':unittest.main()
