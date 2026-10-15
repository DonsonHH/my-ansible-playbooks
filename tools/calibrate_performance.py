#!/usr/bin/env python3
"""Paired, same-host original/candidate measurements. Run in isolated netns."""
import argparse
import json
import statistics
from calibrate_xray import run

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--original',default='/usr/local/bin/xray');p.add_argument('--candidate',required=True);p.add_argument('--pairs',type=int,default=5);p.add_argument('--count',type=int,default=64);args=p.parse_args()
    results={'original':[],'candidate':[]}
    for pair in range(args.pairs):
        order=['original','candidate'] if pair%2==0 else ['candidate','original']
        for name in order:
            value=run(args.original if name=='original' else args.candidate,name=='candidate',args.count,benchmark=True);results[name].append(value)
            print(json.dumps({'stage':'performance-sample','pair':pair,'kind':name,**{k:v for k,v in value.items() if k!='firstBytes'}}),flush=True)
    med={name:{key:statistics.median(row[key] for row in values) for key in ['seconds','cpu','rss']} for name,values in results.items()}
    for name,values in results.items():
        timings=sorted(t for row in values for t in row['firstBytes']);med[name]['firstByteP95']=timings[int((len(timings)-1)*.95)]
    a,b=med['original'],med['candidate'];checks={'firstByteP95Ms':b['firstByteP95']-a['firstByteP95']<=5,'throughputLoss':a['seconds']/b['seconds']>=.9,'cpuIncrease':b['cpu']<=a['cpu']*1.15,'extraMemory':b['rss']-a['rss']<=64*1024*1024}
    print(json.dumps({'stage':'performance-gate','passed':all(checks.values()),'checks':checks,'median':med,'cpuRatio':b['cpu']/max(.001,a['cpu']),'throughputRatio':a['seconds']/b['seconds']}),flush=True)
    raise SystemExit(0 if all(checks.values()) else 1)
