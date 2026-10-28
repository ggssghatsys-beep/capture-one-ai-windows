"""Read-only check of the installed owner and current C1 selection."""
import json
from c1ai.client import call

if __name__=='__main__':
    context=call({'command':'context'})
    print(json.dumps(context,ensure_ascii=False,indent=2))
    raise SystemExit(0 if context.get('status')=='ok' and context.get('editing_ready') else 1)
