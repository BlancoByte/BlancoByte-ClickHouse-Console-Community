#!/usr/bin/env python3
"""
ch_schema.py — BlancoByte ClickHouse Schema Explorer
Standalone CLI to inspect databases, tables, columns, DDL, sizes.

Usage:
  python3 ch_schema.py [--host HOST] [--user USER] [--password PW]
                       [command] [args...]

Commands:
  databases                   List all databases with table counts
  tables  <database>          List tables in database with sizes
  columns <database> <table>  Show columns with types and key flags
  ddl     <database> <table>  Print CREATE TABLE statement
  top     [--by bytes|rows]   Top 20 largest tables cluster-wide
  find    <pattern>           Search tables matching name pattern

Examples:
  python3 ch_schema.py databases
  python3 ch_schema.py tables mydb
  python3 ch_schema.py columns mydb orders
  python3 ch_schema.py ddl mydb orders
  python3 ch_schema.py top --by bytes
  python3 ch_schema.py find user
"""
import argparse, sys
try:
    import clickhouse_connect
except ImportError:
    sys.exit("pip install clickhouse-connect")

try:
    from rich.console import Console
    from rich.table import Table
    from rich.syntax import Syntax
    from rich import box
    RICH=True; con=Console()
except ImportError:
    RICH=False

def fmt_b(b):
    b=float(b or 0)
    for u,t in [('TB',1e12),('GB',1e9),('MB',1e6),('KB',1e3)]:
        if b>=t: return f'{b/t:.2f} {u}'
    return f'{b:.0f} B'

def fmt_n(n):
    n=int(n or 0)
    if n>=1e9: return f'{n/1e9:.2f}B'
    if n>=1e6: return f'{n/1e6:.1f}M'
    if n>=1e3: return f'{n/1e3:.1f}k'
    return str(n)

def gc(args):
    return clickhouse_connect.get_client(
        host=args.host, port=args.port,
        username=args.user, password=args.password or '',
        connect_timeout=5, query_limit=0)

def cmd_databases(cl, args):
    rows=cl.query("""
        SELECT d.name, count(t.name) as tables
        FROM system.databases d
        LEFT JOIN system.tables t ON t.database=d.name
        GROUP BY d.name ORDER BY d.name
    """).result_rows
    if RICH:
        t=Table(title='Databases',box=box.SIMPLE_HEAVY,border_style='dim blue')
        t.add_column('Database',style='cyan bold'); t.add_column('Tables',justify='right')
        for r in rows: t.add_row(r[0],str(r[1]))
        con.print(t)
    else:
        for r in rows: print(f"  {r[0]:<40} {r[1]} tables")

def cmd_tables(cl, args):
    db=args.db or args.args[0] if args.args else None
    if not db: sys.exit("Usage: tables <database>")
    rows=cl.query(f"""
        SELECT t.name, t.engine, sum(p.rows), sum(p.bytes_on_disk), count(p.part_count)
        FROM system.tables t
        LEFT JOIN (SELECT table,database,rows,bytes_on_disk,1 as part_count FROM system.parts WHERE active) p
          ON p.table=t.name AND p.database=t.database
        WHERE t.database='{db}'
        GROUP BY t.name,t.engine ORDER BY sum(p.bytes_on_disk) DESC
    """).result_rows
    if RICH:
        t=Table(title=f'Tables in {db}',box=box.SIMPLE_HEAVY,border_style='dim blue')
        for col,j in [('Table','left'),('Engine','left'),('Rows','right'),('On Disk','right'),('Parts','right')]:
            t.add_column(col,justify=j)
        for r in rows:
            t.add_row(r[0],str(r[1] or ''),fmt_n(r[2] or 0),fmt_b(r[3] or 0),str(r[4] or 0))
        con.print(t)
    else:
        print(f"{'Table':<40} {'Engine':<25} {'Rows':>12} {'Disk':>12}")
        for r in rows: print(f"  {r[0]:<40} {str(r[1] or ''):<25} {fmt_n(r[2] or 0):>12} {fmt_b(r[3] or 0):>12}")

def cmd_columns(cl, args):
    extra=args.args or []
    db=args.db or (extra[0] if len(extra)>0 else None)
    tbl=args.table or (extra[1] if len(extra)>1 else None)
    if not db or not tbl: sys.exit("Usage: columns <database> <table>")
    rows=cl.query(f"""
        SELECT name,type,default_kind,default_expression,
               is_in_partition_key,is_in_sorting_key,is_in_primary_key,
               compression_codec,comment
        FROM system.columns WHERE database='{db}' AND table='{tbl}' ORDER BY position
    """).result_rows
    if RICH:
        t=Table(title=f'Columns: {db}.{tbl}',box=box.SIMPLE_HEAVY,border_style='dim blue')
        t.add_column('Column',style='cyan'); t.add_column('Type',style='yellow')
        t.add_column('Keys'); t.add_column('Default'); t.add_column('Codec'); t.add_column('Comment',style='dim')
        for r in rows:
            keys=''
            if r[6]: keys+='[PK]'
            if r[5]: keys+='[SK]'
            if r[4]: keys+='[PT]'
            t.add_row(r[0],r[1],keys,f"{r[2]}: {r[3]}" if r[2] else '',r[7] or '',r[8] or '')
        con.print(t)
    else:
        print(f"{'Column':<40} {'Type':<35} Keys")
        for r in rows:
            keys=''.join([('P' if r[6] else ''),('S' if r[5] else ''),('T' if r[4] else '')])
            print(f"  {r[0]:<40} {r[1]:<35} {keys}")

def cmd_ddl(cl, args):
    extra=args.args or []
    db=args.db or (extra[0] if len(extra)>0 else None)
    tbl=args.table or (extra[1] if len(extra)>1 else None)
    if not db or not tbl: sys.exit("Usage: ddl <database> <table>")
    rows=cl.query(f"SHOW CREATE TABLE `{db}`.`{tbl}`").result_rows
    ddl=rows[0][0] if rows else ''
    if RICH:
        con.print(Syntax(ddl,'sql',theme='monokai',line_numbers=True))
    else:
        print(ddl)

def cmd_top(cl, args):
    by=getattr(args,'by','bytes') or 'bytes'
    order='sum(p.bytes_on_disk)' if by=='bytes' else 'sum(p.rows)'
    rows=cl.query(f"""
        SELECT t.database, t.name, t.engine, sum(p.rows), sum(p.bytes_on_disk)
        FROM system.tables t
        JOIN system.parts p ON p.table=t.name AND p.database=t.database AND p.active
        WHERE t.database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
        GROUP BY t.database,t.name,t.engine ORDER BY {order} DESC LIMIT 20
    """).result_rows
    if RICH:
        t=Table(title=f'Top 20 Tables by {by.capitalize()}',box=box.SIMPLE_HEAVY,border_style='dim blue')
        t.add_column('Database',style='dim'); t.add_column('Table',style='cyan bold')
        t.add_column('Engine'); t.add_column('Rows',justify='right'); t.add_column('On Disk',justify='right')
        for r in rows: t.add_row(r[0],r[1],r[2],fmt_n(r[3]),fmt_b(r[4]))
        con.print(t)
    else:
        for r in rows: print(f"  {r[0]}.{r[1]:<45} {fmt_n(r[3]):>10} rows  {fmt_b(r[4]):>12}")

def cmd_find(cl, args):
    pattern=args.pattern or (args.args[0] if args.args else None)
    if not pattern: sys.exit("Usage: find <pattern>")
    rows=cl.query(f"""
        SELECT database, name, engine
        FROM system.tables
        WHERE name ILIKE '%{pattern}%'
        ORDER BY database,name LIMIT 50
    """).result_rows
    if RICH:
        t=Table(title=f'Tables matching: {pattern}',box=box.SIMPLE_HEAVY)
        t.add_column('Database',style='dim'); t.add_column('Table',style='cyan'); t.add_column('Engine')
        for r in rows: t.add_row(r[0],r[1],r[2])
        con.print(t)
    else:
        for r in rows: print(f"  {r[0]}.{r[1]:<50} {r[2]}")

CMDS={'databases':cmd_databases,'tables':cmd_tables,'columns':cmd_columns,
      'ddl':cmd_ddl,'top':cmd_top,'find':cmd_find}

def main():
    ap=argparse.ArgumentParser(description='ClickHouse schema explorer')
    ap.add_argument('command',nargs='?',default='databases',choices=list(CMDS)+[''])
    ap.add_argument('args',nargs='*')
    ap.add_argument('--host',default='localhost'); ap.add_argument('--port',type=int,default=8123)
    ap.add_argument('--user',default='default'); ap.add_argument('--password',default='')
    ap.add_argument('--db'); ap.add_argument('--table')
    ap.add_argument('--by',default='bytes',choices=['bytes','rows'])
    ap.add_argument('--pattern')
    a=ap.parse_args()
    cmd=a.command or 'databases'
    # allow positional: tables mydb → args.args=['mydb']
    if a.args and not a.db: a.db=a.args[0] if a.args else None
    if a.args and len(a.args)>1 and not a.table: a.table=a.args[1]
    try:
        cl=gc(a)
        print(f"Connected: ClickHouse {cl.server_version} @ {a.host}:{a.port}\n")
    except Exception as e:
        sys.exit(f"Connection failed: {e}")
    if cmd not in CMDS: sys.exit(f"Unknown command: {cmd}")
    CMDS[cmd](cl,a)

if __name__=='__main__':
    main()
