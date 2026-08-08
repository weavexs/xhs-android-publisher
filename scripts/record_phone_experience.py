#!/usr/bin/env python3
"""Record verified phone UI traces without expanding execution authority."""
from __future__ import annotations
import argparse, hashlib, json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
RESTRICTED={"publish","schedule","delete","privacy","login","captcha","payment","account_policy"}
def main():
    p=argparse.ArgumentParser(); p.add_argument("--workflow",required=True); p.add_argument("--interface",required=True); p.add_argument("--outcome",choices=["success","failed","skipped"],required=True); p.add_argument("--actions-json",default="[]"); p.add_argument("--article-id",default=""); p.add_argument("--evidence",action="append",default=[]); p.add_argument("--root",default=str(Path.home()/".local/share/codex/xhs-ui-tars-experience")); a=p.parse_args()
    actions=json.loads(a.actions_json)
    if not isinstance(actions,list): raise SystemExit("actions-json 必须是数组")
    category=a.workflow.lower().replace("-","_"); restricted=category in RESTRICTED or any(term in category for term in RESTRICTED); now=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    fingerprint=hashlib.sha256(json.dumps({"interface":a.interface,"actions":actions},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    record={"schema_version":"1.0.0","recorded_at":now,"learning_scope":"all_phone_operations_and_interfaces","workflow":a.workflow,"interface":a.interface,"article_id":a.article_id,"outcome":a.outcome,"actions":actions,"evidence":a.evidence,"page_fingerprint":fingerprint,"restricted_operation":restricted,"execution_policy":"observe_locate_prompt_validate_only" if restricted else "deterministic_reuse_candidate","model_execution_allowed":False if restricted else None}
    root=Path(a.root).expanduser().resolve()/"observations"; root.mkdir(parents=True,exist_ok=True); target=root/f"{now[:10]}.jsonl"
    with target.open("a",encoding="utf-8") as h: h.write(json.dumps(record,ensure_ascii=False)+"\n")
    print(json.dumps({"ok":True,"record":str(target),"restricted_operation":restricted,"execution_policy":record["execution_policy"]},ensure_ascii=False,indent=2))
if __name__=="__main__": main()
