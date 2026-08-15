#!/usr/bin/env python3
"""
ch_monitor.py — BlancoByte ClickHouse Terminal Monitor
Standalone real-time monitor: processes, metrics, merges, replication queue.

Usage:
  python3 ch_monitor.py [--host HOST] [--port PORT] [--user USER] [--password PW]
                        [--interval N] [--mode processes|metrics|merges|all]
                        [--kill QUERY_ID] [--alert-threshold SEC]

Examples:
  python3 ch_monitor.py
  python3 ch_monitor.py --host prod-ch --interval 5
  python3 ch_monitor.py --mode merges
  python3 ch_monitor.py --kill abc123de
"""
import argparse, os, sys, time, datetime
try:
    import clickhouse_connect
except ImportError:
    sys.exit("pip install clickhouse-connect")

# ── colour helpers ────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.live import Live
    from rich.columns import Columns
    from rich.text import Text
    from rich import box
    RICH=True
except ImportError:
    RICH=False

def fmt_b(b):
    b=float(b or 0)
    for u,t in [('TB',1e12),('GB',1e9),('MB',1e6),('KB',1e3)]:
        if b>=t: return f'{b/t:.2f} {u}'
    return f'{b:.0f} B'

def fmt_n(n):
    n=float(n or 0)
    if n>=1e9: return f'{n/1e9:.2f}B'
    if n>=1e6: return f'{n/1e6:.1f}M'
    if n>=1e3: return f'{n/1e3:.1f}k'
    return f'{n:.0f}'

def fmt_t(s):
    s=float(s or 0)
    if s>=3600: return f'{int(s//3600)}h{int((s%3600)//60)}m'
    if s>=60:   return f'{int(s//60)}m{int(s%60)}s'
    if s>=1:    return f'{s:.2f}s'
    if s>=0.001:return f'{s*1000:.1f}ms'
    return f'{s*1e6:.0f}μs'

def get_client(args):
    return clickhouse_connect.get_client(
        host=args.host, port=args.port,
        username=args.user, password=args.password or '',
        connect_timeout=5, query_limit=0)

def query(cl, sql):
    try: return cl.query(sql).result_rows
    except Exception as e: return [('ERROR', str(e))]

def show_processes(cl, alert_sec=30):
    rows = query(cl, """
        SELECT query_id, user, elapsed, rows_read, memory_usage,
               formatReadableSize(memory_usage), query
        FROM system.processes ORDER BY elapsed DESC
    """)
    if not RICH:
        print(f"\n{'='*80}\nACTIVE QUERIES  {datetime.datetime.now().strftime('%H:%M:%S')}")
        print(f"{'Query ID':<12} {'User':<12} {'Elapsed':>8} {'Rows':>10} {'Mem':>10}  Query")
        print('-'*80)
        for r in rows:
            qid,user,elapsed,rows,mem,mem_str,q=r
            alert=' ⚠' if float(elapsed)>alert_sec else ''
            print(f"{str(qid)[:12]:<12} {str(user)[:12]:<12} {fmt_t(elapsed):>8} {fmt_n(rows):>10} {str(mem_str):>10}  {str(q)[:60]}{alert}")
        if not rows: print("  (no active queries)")
        return
    t=Table(title=f"Active Queries — {datetime.datetime.now().strftime('%H:%M:%S')}",
            box=box.SIMPLE_HEAVY, border_style="dim blue")
    for col in ('Query ID','User','Elapsed','Rows','Memory','Query'): t.add_column(col)
    for r in rows:
        qid,user,elapsed,rows,mem,mem_str,q=r
        el=float(elapsed)
        e_str=Text(fmt_t(el), style='red bold' if el>alert_sec else ('yellow' if el>10 else 'green'))
        t.add_row(str(qid)[:12],str(user),e_str,fmt_n(rows),str(mem_str),str(q).replace('\n',' ')[:80])
    return t

def show_metrics(cl):
    metrics = {r[0]:float(r[1]) for r in query(cl,"""
        SELECT metric,value FROM system.metrics
        WHERE metric IN ('Query','BackgroundMergesAndMutationsPoolTask',
                         'MemoryTracking','HTTPConnection','TCPConnection','PartsActive')
    """)}
    async_m = {r[0]:float(r[1]) for r in query(cl,"""
        SELECT metric,value FROM system.asynchronous_metrics
        WHERE metric IN ('MemoryResident','OSMemoryTotal','FilesystemMainPathUsedBytes',
                         'FilesystemMainPathTotalBytes','TotalBytesOfMergeTreeTables',
                         'TotalRowsOfMergeTreeTables','NumberOfTables','Uptime')
    """)}
    mem_pct = (async_m.get('MemoryResident',0)/async_m.get('OSMemoryTotal',1)*100) if async_m.get('OSMemoryTotal') else 0
    disk_pct= (async_m.get('FilesystemMainPathUsedBytes',0)/async_m.get('FilesystemMainPathTotalBytes',1)*100) if async_m.get('FilesystemMainPathTotalBytes') else 0
    data=[
        ('Active Queries',   int(metrics.get('Query',0)),      ''),
        ('BG Merges',        int(metrics.get('BackgroundMergesAndMutationsPoolTask',0)),''),
        ('Active Parts',     int(metrics.get('PartsActive',0)),''),
        ('Memory Used',      f"{mem_pct:.1f}%",                fmt_b(async_m.get('MemoryResident',0))+'/'+fmt_b(async_m.get('OSMemoryTotal',0))),
        ('Disk Used',        f"{disk_pct:.1f}%",               fmt_b(async_m.get('FilesystemMainPathUsedBytes',0))+'/'+fmt_b(async_m.get('FilesystemMainPathTotalBytes',0))),
        ('MergeTree Data',   fmt_b(async_m.get('TotalBytesOfMergeTreeTables',0)),''),
        ('Total Rows',       fmt_n(async_m.get('TotalRowsOfMergeTreeTables',0)),''),
        ('Tables',           int(async_m.get('NumberOfTables',0)),''),
        ('Uptime',           fmt_t(async_m.get('Uptime',0)),   ''),
    ]
    if not RICH:
        print(f"\n{'='*50}\nSYSTEM METRICS  {datetime.datetime.now().strftime('%H:%M:%S')}")
        for k,v,extra in data: print(f"  {k:<22} {str(v):>12}  {extra}")
        return
    t=Table(box=box.SIMPLE, border_style='dim blue', title=f"System Metrics — {datetime.datetime.now().strftime('%H:%M:%S')}")
    t.add_column('Metric'); t.add_column('Value',justify='right'); t.add_column('Detail',style='dim')
    for k,v,extra in data:
        t.add_row(k, Text(str(v), style='cyan bold'), extra)
    return t

def show_merges(cl):
    rows=query(cl,"""
        SELECT database,table,elapsed,progress,num_parts,is_mutation,
               formatReadableSize(total_size_bytes_compressed)
        FROM system.merges ORDER BY elapsed DESC
    """)
    if not RICH:
        print(f"\n{'='*80}\nACTIVE MERGES")
        for r in rows: print(f"  {r[0]}.{r[1]}  elapsed={fmt_t(r[2])} progress={float(r[3])*100:.1f}% size={r[6]}")
        if not rows: print("  (no active merges)")
        return
    t=Table(title="Active Merges",box=box.SIMPLE_HEAVY,border_style='dim blue')
    for col in ('Table','Elapsed','Progress','Parts','Size','Type'): t.add_column(col)
    for r in rows:
        prog=float(r[3])*100
        bar='█'*int(prog/10)+'░'*(10-int(prog/10))
        t.add_row(f"{r[0]}.{r[1]}",fmt_t(r[2]),f"{bar} {prog:.0f}%",str(r[4]),str(r[6]),'Mutation' if r[5] else 'Merge')
    return t

def show_replication(cl):
    rows=query(cl,"""
        SELECT database,table,type,source_replica,is_currently_executing,num_tries,exception
        FROM system.replication_queue ORDER BY create_time DESC LIMIT 20
    """)
    if not RICH:
        print(f"\n{'='*80}\nREPLICATION QUEUE ({len(rows)} entries)")
        for r in rows: print(f"  {r[0]}.{r[1]}  type={r[2]} source={r[3]} executing={r[4]} tries={r[5]}")
        if not rows: print("  (queue empty)")
        return
    t=Table(title=f"Replication Queue ({len(rows)} entries)",box=box.SIMPLE_HEAVY,border_style='dim blue')
    for col in ('Table','Type','Source','Executing','Tries','Exception'): t.add_column(col)
    for r in rows:
        t.add_row(f"{r[0]}.{r[1]}",str(r[2]),str(r[3]),'✓' if r[4] else '–',str(r[5]),(str(r[6]) or '')[:60])
    return t

def kill_query(cl, qid):
    cl.command(f"KILL QUERY WHERE query_id = '{qid}' ASYNC")
    print(f"Kill signal sent to query: {qid}")

def run_once(args, cl, mode):
    if not RICH:
        if mode in ('processes','all'): show_processes(cl, args.alert_threshold)
        if mode in ('metrics','all'):   show_metrics(cl)
        if mode in ('merges','all'):    show_merges(cl)
        if mode in ('replication','all'): show_replication(cl)
        return
    con=Console()
    if mode in ('processes','all'):
        t=show_processes(cl,args.alert_threshold)
        if t: con.print(t)
    if mode in ('metrics','all'):
        t=show_metrics(cl)
        if t: con.print(t)
    if mode in ('merges','all'):
        t=show_merges(cl)
        if t: con.print(t)
    if mode in ('replication','all'):
        t=show_replication(cl)
        if t: con.print(t)

def main():
    ap=argparse.ArgumentParser(description='ClickHouse terminal monitor')
    ap.add_argument('--host',default='localhost')
    ap.add_argument('--port',type=int,default=8123)
    ap.add_argument('--user',default='default')
    ap.add_argument('--password',default='')
    ap.add_argument('--interval',type=float,default=0,help='Auto-refresh interval (sec). 0=once')
    ap.add_argument('--mode',default='all',choices=['all','processes','metrics','merges','replication'])
    ap.add_argument('--kill',metavar='QUERY_ID',help='Kill a query by ID and exit')
    ap.add_argument('--alert-threshold',type=int,default=30,help='Seconds to flag long-running queries')
    args=ap.parse_args()
    try:
        cl=get_client(args)
        ver=cl.server_version
        print(f"Connected: ClickHouse {ver} @ {args.host}:{args.port}")
    except Exception as e:
        sys.exit(f"Connection failed: {e}")
    if args.kill:
        kill_query(cl, args.kill)
        return
    if args.interval<=0:
        run_once(args,cl,args.mode)
        return
    print(f"Auto-refresh every {args.interval}s  (Ctrl+C to stop)\n")
    try:
        while True:
            os.system('clear')
            run_once(args,cl,args.mode)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")

if __name__=='__main__':
    main()
