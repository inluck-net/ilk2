import json,sys,re,glob,collections,datetime,shlex,os
# usage: worker_tests.py <label> <runs_dir>
label,runs=sys.argv[1],sys.argv[2]
TEST=re.compile(r'\b(pytest|vitest|bun (run )?test|npm (run )?test|pnpm test|bats)\b')
def ts(s):
    try: return datetime.datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
    except Exception: return None
def classify(cmd):
    seg=[s for s in re.split(r'&&|\|\||;|\|',cmd) if TEST.search(s)]
    if not seg: return None
    s=seg[0]
    if re.search(r'\bbun (run )?test|npm (run )?test|pnpm test',s) and not re.search(r'\.(test|spec)\.\w+',s): return 'whole'
    try: toks=shlex.split(s)
    except Exception: toks=s.split()
    files=[t for t in toks if re.search(r'\.(py|ts|tsx|js|bats)(::|$)',t) or '::' in t]
    dirs=[t for t in toks if not t.startswith('-') and '/' in t and not re.search(r'\.\w+$',t) and 'python' not in t and 'bin/' not in t]
    if files: return 'files'
    if any(t=='-k' for t in toks): return 'files'
    if dirs: return 'dir'
    return 'whole'
agg=collections.defaultdict(lambda:[0,0.0]); iters=0; wall=0.0; slow=[]; bymonth=collections.defaultdict(lambda:[0.0,0.0])
for f in glob.glob(os.path.join(runs,'*','iter-*.log.jsonl')):
    pend={}; first=last=None
    try:
        for line in open(f,errors='replace'):
            try: d=json.loads(line)
            except Exception: continue
            t=ts(d.get('timestamp','')) if d.get('timestamp') else None
            if t: first=first or t; last=t
            m=d.get('message') or {}
            if d.get('parent_tool_use_id'): continue  # root session only
            for c in (m.get('content') or []) if isinstance(m.get('content'),list) else []:
                if c.get('type')=='tool_use' and c.get('name')=='Bash':
                    inp=c.get('input') or {}
                    if inp.get('run_in_background'): continue
                    k=classify(inp.get('command',''))
                    if k and t: pend[c['id']]=(t,k,inp.get('command','')[:100])
                elif c.get('type')=='tool_result' and c.get('tool_use_id') in pend and t:
                    t0,k,cmd=pend.pop(c['tool_use_id']); dur=t-t0
                    if 0<=dur<4*3600:
                        agg[k][0]+=1; agg[k][1]+=dur; slow.append((dur,k,cmd))
                        mo=datetime.datetime.fromtimestamp(t0).strftime('%Y-%m'); bymonth[mo][1]+=dur
    except Exception as e: continue
    if first and last:
        iters+=1; wall+=last-first
        bymonth[datetime.datetime.fromtimestamp(first).strftime('%Y-%m')][0]+=last-first
tot=sum(v[1] for v in agg.values())
print(f"## {label}: {iters} iteration streams, {wall/3600:.1f} h stream wall; worker foreground test time {tot/3600:.1f} h ({100*tot/max(wall,1):.1f}%)")
for k in ('files','dir','whole'):
    n,s=agg[k]; print(f"  {k:5}: {n} calls, {s/3600:.1f} h ({100*s/max(wall,1):.1f}% of wall), mean {s/max(n,1):.0f} s")
print("  by month (wall h, test h, share):", {m:(round(a/3600,1),round(b/3600,1),f"{100*b/max(a,1):.0f}%") for m,(a,b) in sorted(bymonth.items())})
for d,k,c in sorted(slow,reverse=True)[:4]: print(f"  slowest {d:.0f}s {k} {c}")
