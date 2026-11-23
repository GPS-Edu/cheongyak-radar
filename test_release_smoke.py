"""Release smoke checks: real generated payload, publishing contract and UI data wiring."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, date
from pathlib import Path
from market_v49 import analyse
from market_v52 import summarize
from validate_v52 import validate

TODAY=date.today()

class ReleaseSmoke(unittest.TestCase):
    def fixture(self):
        item={'id':'test-001','name':'공급공고 테스트 단지','region':'서울',
              'kind':'무순위','start':TODAY.isoformat(),'end':TODAY.isoformat(),
              'housingTypes':[{'type':'084.0000A','exclusiveAreaSqm':84,'priceEok':18.56}]}
        rows=[{'apartment':f'비교 신축 {i}', 'dealAmountManwon':int(v*10000),
               'exclusiveAreaSqm':84,'dealDate':TODAY.isoformat(), 'buildYear':str(TODAY.year-2),
               'legalDong':'임의동','jibun':str(i)} for i,v in enumerate([40,41])]
        item['marketV49']=analyse(item,rows,[],TODAY)
        item['priceSummaryV52']=summarize(item,item['marketV49'],TODAY)
        return item
    def test_market_pipeline_and_publish(self):
        item=self.fixture()
        self.assertIsNotNone(item['priceSummaryV52']['baseline'])
        self.assertGreater(item['priceSummaryV52']['baseline']['gapEok'],20)
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            (d/'data.preview.json').write_text(json.dumps({'updated_at':datetime.now(timezone.utc).isoformat(),'projects':[item]}))
            validate(str(d/'data.preview.json'),str(d/'data.json'))
            result=json.loads((d/'data.json').read_text())
            self.assertEqual(result['dataSchemaVersion'],52)
            self.assertEqual(result['projects'][0]['priceSummaryV52']['version'],'v52-opportunity-summary')
    def test_bad_card_prevents_overwrite(self):
        item=self.fixture(); item['priceSummaryV52']['baseline']['saleEok']=1000
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);source=d/'data.preview.json';out=d/'data.json'
            source.write_text(json.dumps({'updated_at':datetime.now(timezone.utc).isoformat(),'projects':[item]}))
            out.write_text('PREVIOUS_VALID_DATA')
            with self.assertRaises(RuntimeError): validate(str(source),str(out))
            self.assertEqual(out.read_text(),'PREVIOUS_VALID_DATA')
    def test_price_widget_and_refresh_handlers_exist(self):
        html=Path('index.html').read_text(encoding='utf-8')
        for marker in ('function v50MarketPanel(p)', 'moneyPanel=function(p){return v50MarketPanel(p);}',
                       "fetch('./data.json?ts='+Date.now(),{cache:'no-store'})", "if(oldEvidence)panel=v49Panel(p)"):
            self.assertIn(marker,html)
        self.assertNotIn('<title>청약레이더 v40',html)

if __name__=='__main__':unittest.main()
