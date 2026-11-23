import unittest
from pathlib import Path

class FrontendChecks(unittest.TestCase):
    def test_clean_public_ui(self):
        s=Path('index.html').read_text(encoding='utf-8')
        self.assertNotIn('데이터 갱신 문제 해결 방법',s)
        self.assertNotIn('GitHub Actions에서 최신 수집 워크플로를 실행해',s)
        self.assertIn('function v50MarketPanel(p)',s)
        self.assertIn('if(oldEvidence)panel=v49Panel(p)',s)
        self.assertIn('else if(hasLedger)panel=legacyMoneyPanel(p)',s)
    def test_contract_on_server(self):
        s=Path('fetch_live.py').read_text(encoding='utf-8')
        self.assertIn('project["priceSummaryV52"] = summarize_prices',s)
        self.assertIn('project["marketV49"] = analyse_newbuild',s)
if __name__=='__main__': unittest.main()
