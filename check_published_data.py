"""Check published pricing pipeline; run locally or on GitHub Actions."""
import json
from pathlib import Path

def check(path='data.json'):
    payload=json.loads(Path(path).read_text(encoding='utf-8'))
    rows=payload.get('projects') or []
    schema=payload.get('dataSchemaVersion')
    cards=sum(isinstance(x.get('priceSummaryV52'),dict) and x['priceSummaryV52'].get('version')=='v52-opportunity-summary' for x in rows)
    comparison=sum(bool((x.get('priceSummaryV52') or {}).get('baseline')) for x in rows)
    leads=sum(bool((x.get('priceSummaryV52') or {}).get('headline')) for x in rows)
    print(f'[price health] schema={schema} cards={cards}/{len(rows)} compared={comparison} leads={leads} updated={payload.get("updated_at")}')
    if not rows or cards!=len(rows) or schema!=52:
        raise SystemExit('ERROR: Published data is older than the app pricing contract. The prior data remains until fixed.')
    if comparison==0 and leads==0:
        print('WARNING: the pipeline produced no price comparisons; check trade retrieval/matching logs.')
    return cards,comparison,leads

if __name__=='__main__':check()
