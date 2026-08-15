#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clickhouse_pitr.py  -  ClickHouse Point-in-Time Recovery Tool  v10.0

INSTALL:
    pip install clickhouse-connect rich
    pip install boto3                    # for --storage s3 / minio
    pip install google-cloud-storage     # for --storage gcs
    pip install azure-storage-blob       # for --storage azure

CLICKHOUSE SERVER CONFIG (required before first backup):
    /etc/clickhouse-server/config.d/backup.xml:
        <clickhouse>
            <backups>
                <allowed_path>/var/lib/clickhouse/backups</allowed_path>
            </backups>
        </clickhouse>
    sudo mkdir -p /var/lib/clickhouse/backups
    sudo chown clickhouse:clickhouse /var/lib/clickhouse/backups
    sudo chmod 750 /var/lib/clickhouse/backups
    sudo systemctl restart clickhouse-server

BACKUP TYPES:
    full          Complete snapshot of the table.

    differential  Captures all changes since the last FULL backup.
                  Base is always the latest full backup.
                  Chain: Full -> Diff1 (base:Full) -> Diff2 (base:Full)
                  Restore needs: Full + latest Differential only.
                  Simple and reliable.

    incremental   Captures only changes since the PREVIOUS backup
                  (full or incremental).
                  Base is the most recent backup of any type.
                  Chain: Full -> Incr1 (base:Full) -> Incr2 (base:Incr1)
                  Restore needs: Full + all Incrementals in chain.
                  Smaller backups, more complex restore chain.

HOW RESTORE WORKS:
    Uses: RESTORE TABLE original AS temp FROM backup
    ClickHouse follows the full->differential/incremental chain internally.
    No missing files. No ATTACH PART issues.

    Then:
      1. Wait for pending DELETE/UPDATE mutations to finish
      2. INSERT INTO original SELECT * FROM temp
         EXCEPT SELECT * FROM original
      3. DROP temp table

    The original table NEVER has rows deleted. Only new rows are added.

RESTORE METHOD --method attach (experimental):
    Uses ATTACH PART with dependency resolution.
    Reads checksums.txt, finds missing dict files from base backup,
    copies them before attaching. Only works with --storage local.

COMMANDS:
    backup    Take a full, differential, or incremental backup
    restore   Restore via RESTORE AS + EXCEPT INSERT (default)
              or ATTACH PART with dependency resolution (--method attach)
    list      List backups in catalog
    info      Full details of one backup
    verify    Check integrity
    prune     Delete old backups
    chain     Show backup chain
    schedule  Generate crontab line
    config    Show current configuration

QUICK START:
    # Full backup
    python3 clickhouse_pitr.py backup \\
        --host localhost --port 8123 \\
        --username default --password default \\
        --storage local --backup-dir /var/lib/clickhouse/backups \\
        --tables uk.uk_price_paid

    # Differential backup (base: latest full)
    python3 clickhouse_pitr.py backup \\
        --host localhost --port 8123 \\
        --username default --password default \\
        --storage local --backup-dir /var/lib/clickhouse/backups \\
        --tables uk.uk_price_paid --differential --flush-before-backup

    # Incremental backup (base: latest backup of any type)
    python3 clickhouse_pitr.py backup \\
        --host localhost --port 8123 \\
        --username default --password default \\
        --storage local --backup-dir /var/lib/clickhouse/backups \\
        --tables uk.uk_price_paid --incremental --flush-before-backup

    # Restore
    python3 clickhouse_pitr.py restore \\
        --host localhost --port 8123 \\
        --username default --password default \\
        --storage local --backup-dir /var/lib/clickhouse/backups \\
        --backup-id backup_20260331_112416

ENVIRONMENT VARIABLES:
    CH_HOST, CH_PORT, CH_USER, CH_PASSWORD, CH_DATABASE
    CH_BACKUP_DIR, CH_CATALOG_FILE, CH_STORAGE
    CH_COMPRESS, CH_PARALLEL_THREADS, CH_VERIFY_AFTER_BACKUP
    CH_MAX_RETRIES, CH_RETRY_DELAY
    S3_BUCKET, S3_PREFIX, S3_REGION, S3_ENDPOINT
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_PROFILE
    GCS_BUCKET, GCS_CREDENTIALS
    AZURE_ACCOUNT, AZURE_CONTAINER, AZURE_SAS_TOKEN, AZURE_CONNECTION_STRING
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box as rbox
    RICH = True
    console = Console(highlight=False)
except ImportError:
    RICH = False
    console = None

try:
    import clickhouse_connect
    CH_OK = True
except ImportError:
    CH_OK = False

try:
    import boto3
    BOTO3_OK = True
except ImportError:
    BOTO3_OK = False

try:
    from google.cloud import storage as gcs_lib
    GCS_OK = True
except ImportError:
    GCS_OK = False

try:
    from azure.storage.blob import BlobServiceClient
    AZURE_OK = True
except ImportError:
    AZURE_OK = False

VERSION     = "10.0.0"
CATALOG_VER = 10
DEFAULT_DIR = "./ch_backups"
DEFAULT_CAT = "./ch_backups/catalog.json"
log         = logging.getLogger("ch_pitr")


class PITRError(Exception):    pass
class ConfigError(PITRError):  pass
class BackupError(PITRError):  pass
class RestoreError(PITRError): pass
class CatalogError(PITRError): pass
class VerifyError(PITRError):  pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    host:            str  = "localhost"
    port:            int  = 8123
    user:            str  = "default"
    password:        str  = ""
    database:        str  = "default"
    connect_timeout: int  = 10
    query_timeout:   int  = 3600
    backup_dir:      str  = DEFAULT_DIR
    catalog:         str  = DEFAULT_CAT
    storage:         str  = "local"
    s3_bucket:       str  = ""
    s3_prefix:       str  = "clickhouse-backups"
    s3_region:       str  = "us-east-1"
    s3_endpoint:     str  = ""
    s3_access_key:   str  = ""
    s3_secret_key:   str  = ""
    s3_path_style:   bool = False
    gcs_bucket:      str  = ""
    gcs_credentials: str  = ""
    azure_account:   str  = ""
    azure_container: str  = ""
    azure_sas_token: str  = ""
    azure_conn_str:  str  = ""
    compress:        bool = True
    parallel:        int  = 4
    verify:          bool = True
    max_retries:     int  = 3
    retry_delay:     int  = 5

    @classmethod
    def from_env(cls):
        return cls(
            host            = os.getenv("CH_HOST",     "localhost"),
            port            = int(os.getenv("CH_PORT", "8123")),
            user            = os.getenv("CH_USER",     "default"),
            password        = os.getenv("CH_PASSWORD", ""),
            database        = os.getenv("CH_DATABASE", "default"),
            backup_dir      = os.getenv("CH_BACKUP_DIR",   DEFAULT_DIR),
            catalog         = os.getenv("CH_CATALOG_FILE", DEFAULT_CAT),
            storage         = os.getenv("CH_STORAGE",      "local"),
            s3_bucket       = os.getenv("S3_BUCKET",       ""),
            s3_prefix       = os.getenv("S3_PREFIX",       "clickhouse-backups"),
            s3_region       = os.getenv("S3_REGION",       "us-east-1"),
            s3_endpoint     = os.getenv("S3_ENDPOINT",     ""),
            s3_access_key   = os.getenv("AWS_ACCESS_KEY_ID",      ""),
            s3_secret_key   = os.getenv("AWS_SECRET_ACCESS_KEY",  ""),
            gcs_bucket      = os.getenv("GCS_BUCKET",      ""),
            gcs_credentials = os.getenv("GCS_CREDENTIALS", ""),
            azure_account   = os.getenv("AZURE_ACCOUNT",           ""),
            azure_container = os.getenv("AZURE_CONTAINER",         ""),
            azure_sas_token = os.getenv("AZURE_SAS_TOKEN",         ""),
            azure_conn_str  = os.getenv("AZURE_CONNECTION_STRING", ""),
            compress     = os.getenv("CH_COMPRESS",            "true").lower() == "true",
            parallel     = int(os.getenv("CH_PARALLEL_THREADS","4")),
            verify       = os.getenv("CH_VERIFY_AFTER_BACKUP", "true").lower() == "true",
            max_retries  = int(os.getenv("CH_MAX_RETRIES", "3")),
            retry_delay  = int(os.getenv("CH_RETRY_DELAY", "5")),
        )

    def apply(self, args):
        pairs = [
            ("host","host"), ("port","port"), ("user","username"),
            ("password","password"), ("database","database"),
            ("backup_dir","backup_dir"), ("catalog","catalog_file"),
            ("s3_bucket","s3_bucket"), ("s3_prefix","s3_prefix"),
            ("s3_region","s3_region"), ("s3_endpoint","s3_endpoint"),
            ("s3_access_key","s3_access_key"), ("s3_secret_key","s3_secret_key"),
            ("s3_path_style","s3_path_style"),
            ("gcs_bucket","gcs_bucket"), ("gcs_credentials","gcs_credentials"),
            ("azure_account","azure_account"), ("azure_container","azure_container"),
            ("azure_sas_token","azure_sas_token"), ("azure_conn_str","azure_conn_str"),
            ("parallel","parallel_threads"),
        ]
        for ck, ak in pairs:
            v = getattr(args, ak, None)
            if v is not None:
                setattr(self, ck, v)
        if getattr(args, "storage", None):
            self.storage = args.storage
        if getattr(args, "no_compress", False):
            self.compress = False
        if getattr(args, "no_verify", False):
            self.verify = False
        return self

    def validate(self):
        errs = []
        if not CH_OK:
            errs.append("clickhouse-connect not installed  ->  pip install clickhouse-connect")
        if self.storage == "s3":
            if not self.s3_bucket:
                errs.append("--storage s3 requires --s3-bucket")
            if not BOTO3_OK:
                errs.append("boto3 not installed  ->  pip install boto3")
        if self.storage == "gcs":
            if not self.gcs_bucket:
                errs.append("--storage gcs requires --gcs-bucket")
            if not GCS_OK:
                errs.append("google-cloud-storage not installed")
        if self.storage == "azure":
            if not self.azure_container:
                errs.append("--storage azure requires --azure-container")
            if not AZURE_OK:
                errs.append("azure-storage-blob not installed")
        if errs:
            raise ConfigError("Configuration errors:\n  - " + "\n  - ".join(errs))

    def storage_label(self):
        if self.storage == "local":
            return "local  ->  " + str(Path(self.backup_dir).resolve())
        if self.storage == "s3":
            ep = " (" + self.s3_endpoint + ")" if self.s3_endpoint else " (AWS S3)"
            return "s3" + ep + "  ->  s3://" + self.s3_bucket + "/" + self.s3_prefix
        if self.storage == "gcs":
            return "gcs  ->  gs://" + self.gcs_bucket
        if self.storage == "azure":
            return "azure  ->  " + self.azure_account + "/" + self.azure_container
        return self.storage


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class TableSnap:
    fqn:        str
    row_count:  int
    size_bytes: int
    checksum:   str

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(fqn=d.get("fqn",""), row_count=d.get("row_count",0),
                   size_bytes=d.get("size_bytes",0), checksum=d.get("checksum",""))


@dataclass
class BackupEntry:
    backup_id:        str
    timestamp:        str
    tables:           list
    storage_backend:  str
    storage_path:     str
    backup_type:      str   = "full"      # full | differential | incremental
    base_backup_id:   str   = ""
    status:           str   = "running"
    size_bytes:       int   = 0
    duration_seconds: float = 0.0
    ch_version:       str   = ""
    note:             str   = ""
    tag:              str   = ""
    table_snaps:      list  = field(default_factory=list)
    checksum_master:  str   = ""
    catalog_version:  int   = CATALOG_VER
    s3_bucket:        str   = ""
    s3_prefix:        str   = ""
    s3_region:        str   = ""
    s3_endpoint:      str   = ""
    gcs_bucket:       str   = ""
    azure_account:    str   = ""
    azure_container:  str   = ""

    @property
    def ts_dt(self):
        return datetime.fromisoformat(self.timestamp).replace(tzinfo=timezone.utc)

    @property
    def size_h(self): return _hbytes(self.size_bytes)

    @property
    def dur_h(self): return _hdur(self.duration_seconds)

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = set(cls.__dataclass_fields__.keys())
        fd = {k: v for k, v in d.items() if k in known}
        defs = {
            "backup_type":"full","base_backup_id":"","catalog_version":1,
            "table_snaps":[],"checksum_master":"","tag":"","storage_backend":"local",
            "s3_bucket":"","s3_prefix":"","s3_region":"","s3_endpoint":"",
            "gcs_bucket":"","azure_account":"","azure_container":"",
        }
        for k, v in defs.items():
            fd.setdefault(k, v)
        return cls(**fd)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class Catalog:
    def __init__(self, path):
        self.path = Path(path)
        self._entries = []
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            recs = raw.get("entries", raw) if isinstance(raw, dict) else raw
            self._entries = [BackupEntry.from_dict(r) for r in recs]
        except Exception as e:
            raise CatalogError("Cannot read catalog: " + str(e))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "catalog_version": CATALOG_VER,
            "tool_version":    VERSION,
            "last_updated":    _now_iso(),
            "count":           len(self._entries),
            "entries":         [e.to_dict() for e in self._entries],
        }, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    def add(self, e):
        self._entries.append(e); self._save()

    def update(self, e):
        for i, x in enumerate(self._entries):
            if x.backup_id == e.backup_id:
                self._entries[i] = e; self._save(); return
        raise CatalogError("Entry not found: " + e.backup_id)

    def get(self, backup_id):
        return next((e for e in self._entries if e.backup_id == backup_id), None)

    def list_all(self, status=None):
        es = self._entries if not status else [e for e in self._entries if e.status == status]
        return sorted(es, key=lambda e: e.timestamp, reverse=True)

    def find_best_for(self, target_dt):
        ok = {"ok", "verified"}
        cands = [e for e in self._entries if e.status in ok and e.ts_dt <= target_dt]
        return max(cands, key=lambda e: e.timestamp) if cands else None

    def walk_chain(self, entry):
        chain, cur, visited = [], entry, set()
        while cur:
            if cur.backup_id in visited: break
            visited.add(cur.backup_id); chain.append(cur)
            cur = self.get(cur.base_backup_id) if cur.base_backup_id else None
        chain.reverse()
        return chain

    def find_chain_for(self, target_dt):
        best = self.find_best_for(target_dt)
        return self.walk_chain(best) if best else []

    def find_latest_full(self):
        """Return latest successful full backup. Used as base for differential."""
        ok = {"ok", "verified"}
        fulls = [e for e in self._entries if e.status in ok and e.backup_type == "full"]
        return max(fulls, key=lambda e: e.timestamp) if fulls else None

    def find_latest_any(self):
        """Return latest successful backup of any type. Used as base for incremental."""
        ok = {"ok", "verified"}
        cands = [e for e in self._entries if e.status in ok]
        return max(cands, key=lambda e: e.timestamp) if cands else None

    def prune_by_days(self, keep_days):
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        ok = {"ok", "verified"}
        ids = [e.backup_id for e in self._entries if e.status in ok and e.ts_dt < cutoff]
        self._entries = [e for e in self._entries if e.backup_id not in ids]
        self._save(); return ids

    def prune_by_count(self, keep_count):
        ok = {"ok", "verified"}
        oks = sorted([e for e in self._entries if e.status in ok],
                     key=lambda e: e.timestamp, reverse=True)
        ids = [e.backup_id for e in oks[keep_count:]]
        self._entries = [e for e in self._entries if e.backup_id not in ids]
        self._save(); return ids


# ---------------------------------------------------------------------------
# Storage driver
# ---------------------------------------------------------------------------
class Storage:
    def __init__(self, cfg):
        self.cfg = cfg

    def ch_dest(self, backup_id):
        c = self.cfg
        if c.storage == "local":
            return "File('" + str(Path(c.backup_dir).resolve() / backup_id) + "')"
        if c.storage == "s3":
            if c.s3_endpoint:
                uri = c.s3_endpoint.rstrip("/") + "/" + c.s3_bucket + "/" + c.s3_prefix + "/" + backup_id
            else:
                uri = "s3://" + c.s3_bucket + "/" + c.s3_prefix + "/" + backup_id
            if c.s3_access_key and c.s3_secret_key:
                return "S3('" + uri + "', '" + c.s3_access_key + "', '" + c.s3_secret_key + "')"
            return "S3('" + uri + "')"
        if c.storage == "gcs":
            return "S3('https://storage.googleapis.com/" + c.gcs_bucket + "/" + backup_id + "')"
        if c.storage == "azure":
            conn = c.azure_conn_str or c.azure_account
            return "AzureBlobStorage('" + conn + "', '" + c.azure_container + "', '" + backup_id + "')"
        raise ConfigError("Unknown storage: " + c.storage)

    def local_path(self, backup_id):
        return Path(self.cfg.backup_dir) / backup_id

    def size(self, backup_id):
        if self.cfg.storage == "local":
            p = self.local_path(backup_id)
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0
        if self.cfg.storage == "s3":    return self._s3_size(backup_id)
        if self.cfg.storage == "gcs":   return self._gcs_size(backup_id)
        if self.cfg.storage == "azure": return self._azure_size(backup_id)
        return 0

    def exists(self, backup_id):
        if self.cfg.storage == "local": return self.local_path(backup_id).exists()
        return self.size(backup_id) > 0

    def delete(self, backup_id):
        if self.cfg.storage == "local":
            p = self.local_path(backup_id)
            if p.exists(): shutil.rmtree(p)
        elif self.cfg.storage == "s3":    self._s3_delete(backup_id)
        elif self.cfg.storage == "gcs":   self._gcs_delete(backup_id)
        elif self.cfg.storage == "azure": self._azure_delete(backup_id)

    def _s3c(self):
        if not BOTO3_OK: raise ConfigError("boto3 not installed")
        c = self.cfg
        kw = {"region_name": c.s3_region}
        if c.s3_endpoint: kw["endpoint_url"] = c.s3_endpoint
        sk = {}
        if c.s3_access_key and c.s3_secret_key:
            sk["aws_access_key_id"] = c.s3_access_key
            sk["aws_secret_access_key"] = c.s3_secret_key
        elif os.getenv("AWS_PROFILE"): sk["profile_name"] = os.getenv("AWS_PROFILE")
        return boto3.Session(**sk).client("s3", **kw)

    def _s3k(self, bid): return self.cfg.s3_prefix + "/" + bid

    def _s3_size(self, bid):
        try:
            s3, total = self._s3c(), 0
            for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=self.cfg.s3_bucket, Prefix=self._s3k(bid)):
                for obj in page.get("Contents", []): total += obj["Size"]
            return total
        except Exception: return 0

    def _s3_delete(self, bid):
        try:
            s3 = self._s3c()
            for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=self.cfg.s3_bucket, Prefix=self._s3k(bid)):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs: s3.delete_objects(Bucket=self.cfg.s3_bucket, Delete={"Objects": objs})
        except Exception as e: _warn("S3 delete failed: " + str(e))

    def _gcsc(self):
        if not GCS_OK: raise ConfigError("google-cloud-storage not installed")
        return (gcs_lib.Client.from_service_account_json(self.cfg.gcs_credentials)
                if self.cfg.gcs_credentials else gcs_lib.Client())

    def _gcs_size(self, bid):
        try: return sum(b.size for b in self._gcsc().list_blobs(self.cfg.gcs_bucket, prefix=bid))
        except Exception: return 0

    def _gcs_delete(self, bid):
        try:
            for b in self._gcsc().list_blobs(self.cfg.gcs_bucket, prefix=bid): b.delete()
        except Exception as e: _warn("GCS delete failed: " + str(e))

    def _azc(self):
        if not AZURE_OK: raise ConfigError("azure-storage-blob not installed")
        c = self.cfg
        if c.azure_conn_str: return BlobServiceClient.from_connection_string(c.azure_conn_str)
        return BlobServiceClient(
            account_url="https://" + c.azure_account + ".blob.core.windows.net" + c.azure_sas_token)

    def _azure_size(self, bid):
        try:
            cc = self._azc().get_container_client(self.cfg.azure_container)
            return sum(b["size"] for b in cc.list_blobs(name_starts_with=bid))
        except Exception: return 0

    def _azure_delete(self, bid):
        try:
            cc = self._azc().get_container_client(self.cfg.azure_container)
            for b in cc.list_blobs(name_starts_with=bid): cc.delete_blob(b["name"])
        except Exception as e: _warn("Azure delete failed: " + str(e))


# ---------------------------------------------------------------------------
# ClickHouse client
# ---------------------------------------------------------------------------
class CH:
    def __init__(self, cfg):
        self.cfg = cfg; self._cl = None

    def connect(self):
        if not CH_OK:
            raise ConfigError("clickhouse-connect not installed  ->  pip install clickhouse-connect")
        self._cl = clickhouse_connect.get_client(
            host=self.cfg.host, port=self.cfg.port,
            username=self.cfg.user, password=self.cfg.password,
            database=self.cfg.database,
            connect_timeout=self.cfg.connect_timeout, query_limit=0)

    def ping(self):
        try: self._cl.ping(); return True
        except Exception: return False

    @property
    def version(self):
        try:    return self._cl.server_version
        except: return "?"

    def rows(self, sql): return self._cl.query(sql).result_rows

    def cmd(self, sql):
        log.debug("SQL: %s", sql[:400]); return self._cl.command(sql)

    def tables(self, database=None):
        if database:
            where = "WHERE database='" + database + "'"
        else:
            where = "WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA')"
        return [r[0] + "." + r[1] for r in
                self.rows("SELECT database, name FROM system.tables " + where +
                          " ORDER BY database, name")]

    def snap(self, fqn):
        db, tbl = _fqn(fqn)
        rc  = self.rows("SELECT count() FROM " + db + "." + tbl)
        row_count = rc[0][0] if rc else 0
        sr  = self.rows("SELECT sum(bytes_on_disk) FROM system.parts "
                        "WHERE database='" + db + "' AND table='" + tbl + "' AND active=1")
        size = (sr[0][0] or 0) if sr else 0
        try:
            cr = self.rows("SELECT sipHash64(groupArray(toString((*,)))) "
                           "FROM (SELECT * FROM " + db + "." + tbl + " LIMIT 5000)")
            checksum = str(cr[0][0]) if cr else ""
        except Exception: checksum = ""
        return TableSnap(fqn=fqn, row_count=row_count, size_bytes=size, checksum=checksum)

    def backup_cmd(self, tables, dest, cfg, base_dest=None):
        clause   = ",\n  ".join("TABLE " + t for t in tables)
        settings = []
        if cfg.compress: settings.append("compression_method='lz4', compression_level=3")
        if base_dest:    settings.append("base_backup=" + base_dest)
        sc  = (" SETTINGS " + ", ".join(settings)) if settings else ""
        return self.cmd("BACKUP\n  " + clause + "\nTO " + dest + sc)

    def restore_as(self, src_db, src_tbl, tmp_db, tmp_tbl, dest):
        sql = ("RESTORE TABLE " + src_db + "." + src_tbl +
               " AS " + tmp_db + "." + tmp_tbl + "\nFROM " + dest)
        return self.cmd(sql)

    def table_exists(self, db, tbl):
        rows = self.rows("SELECT count() FROM system.tables "
                         "WHERE database='" + db + "' AND name='" + tbl + "'")
        return bool(rows and rows[0][0] > 0)

    def row_count(self, db, tbl):
        rows = self.rows("SELECT count() FROM " + db + "." + tbl)
        return rows[0][0] if rows else 0

    def drop_table(self, db, tbl):
        self.cmd("DROP TABLE IF EXISTS " + db + "." + tbl)

    def insert_except(self, dst_db, dst_tbl, src_db, src_tbl):
        return self.cmd(
            "INSERT INTO " + dst_db + "." + dst_tbl +
            " SELECT * FROM " + src_db + "." + src_tbl +
            " EXCEPT SELECT * FROM " + dst_db + "." + dst_tbl)

    def wait_mutations(self, db, tbl, timeout=300):
        _out("  Waiting for pending mutations on " + db + "." + tbl + "...")
        start = time.time()
        while time.time() - start < timeout:
            rows = self.rows("SELECT count() FROM system.mutations "
                             "WHERE database='" + db + "' AND table='" + tbl + "' AND is_done=0")
            pending = rows[0][0] if rows else 0
            if pending == 0:
                _out("  All mutations complete."); return
            _out("  " + str(pending) + " mutation(s) pending, waiting 2s...")
            time.sleep(2)
        _warn("  Mutations still pending after " + str(timeout) + "s - proceeding anyway")

    def optimize_final(self, db, tbl):
        _out("  Flushing and optimizing " + db + "." + tbl + " (this may take a moment)...")
        self.cmd("SYSTEM FLUSH LOGS")
        self.cmd("OPTIMIZE TABLE " + db + "." + tbl + " FINAL")

    def column_defs(self, db, tbl):
        return self.rows("SELECT name, type FROM system.columns "
                         "WHERE database='" + db + "' AND table='" + tbl + "' ORDER BY position")

    def attach_part(self, db, tbl, part_name):
        self.cmd("ALTER TABLE " + db + "." + tbl + " ATTACH PART '" + part_name + "'")

    def stop_merges(self, db, tbl):
        self.cmd("SYSTEM STOP MERGES " + db + "." + tbl)

    def start_merges(self, db, tbl):
        self.cmd("SYSTEM START MERGES " + db + "." + tbl)


# ---------------------------------------------------------------------------
# Backup manager
# ---------------------------------------------------------------------------
class BackupManager:
    def __init__(self, cfg):
        self.cfg     = cfg
        self.catalog = Catalog(cfg.catalog)
        self.ch      = CH(cfg)
        self.store   = Storage(cfg)
        Path(cfg.backup_dir).mkdir(parents=True, exist_ok=True)

    def _connect(self):
        self.ch.connect()
        if not self.ch.ping():
            raise BackupError("Cannot connect to ClickHouse at " +
                              self.cfg.host + ":" + str(self.cfg.port))

    def _resolve(self, tables, all_tables, database):
        if all_tables:  return self.ch.tables()
        if database:    return self.ch.tables(database)
        if tables:      return list(tables)
        raise BackupError("Specify --tables, --all-tables, or --database")

    def take(self, tables=None, all_tables=False, database=None,
             note="", tag="",
             backup_mode="full",   # "full" | "differential" | "incremental"
             base_backup_id="",
             flush_before_backup=False):
        """
        Take a backup.

        backup_mode:
          full          Complete snapshot. No base backup needed.
          differential  Changes since latest FULL backup.
                        Chain: Full -> Diff1(base:Full) -> Diff2(base:Full)
                        Restore: Full + this differential only.
          incremental   Changes since latest backup of ANY type.
                        Chain: Full -> Incr1(base:Full) -> Incr2(base:Incr1)
                        Restore: Full + all incrementals in chain.
        """
        self._connect()
        resolved = self._resolve(tables, all_tables, database)
        if not resolved: raise BackupError("No tables found.")

        if backup_mode in ("differential", "incremental") and not base_backup_id:
            if backup_mode == "differential":
                # Base is always the latest FULL backup
                base = self.catalog.find_latest_full()
                if not base:
                    raise BackupError(
                        "No successful full backup found.\n"
                        "  Run a full backup first before taking a differential.")
            else:
                # Base is the latest backup of ANY type (full or incremental)
                base = self.catalog.find_latest_any()
                if not base:
                    raise BackupError(
                        "No successful backup found.\n"
                        "  Run a full backup first before taking an incremental.")
            base_backup_id = base.backup_id
            _out("  Auto-selected base: " + base_backup_id +
                 "  [" + base.backup_type + "]")

        if backup_mode in ("differential", "incremental") and flush_before_backup:
            _out("  --flush-before-backup: forcing final merge on all tables...")
            for fqn in resolved:
                db, tbl = _fqn(fqn)
                self.ch.optimize_final(db, tbl)
            _out("  Flush and optimize complete.")

        now = datetime.now(timezone.utc)
        bid = "backup_" + now.strftime("%Y%m%d_%H%M%S") + ("_" + tag if tag else "")

        entry = BackupEntry(
            backup_id       = bid,
            timestamp       = now.isoformat(),
            tables          = resolved,
            storage_backend = self.cfg.storage,
            storage_path    = bid,
            backup_type     = backup_mode,
            base_backup_id  = base_backup_id,
            status          = "running",
            ch_version      = self.cfg.host + ":" + str(self.cfg.port) + " (CH " + self.ch.version + ")",
            note            = note,
            tag             = tag,
            s3_bucket       = self.cfg.s3_bucket,
            s3_prefix       = self.cfg.s3_prefix,
            s3_region       = self.cfg.s3_region,
            s3_endpoint     = self.cfg.s3_endpoint,
            gcs_bucket      = self.cfg.gcs_bucket,
            azure_account   = self.cfg.azure_account,
            azure_container = self.cfg.azure_container,
        )
        self.catalog.add(entry)

        _hr()
        _out("  Backup ID    : " + bid)
        _out("  Type         : " + backup_mode.upper() +
             ("  (base: " + base_backup_id + ")" if base_backup_id else ""))
        _out("  Tables       : " + ", ".join(resolved))
        _out("  Storage      : " + self.cfg.storage_label())
        _out("  ClickHouse   : " + self.ch.version)
        _out("  Compression  : " + ("LZ4" if self.cfg.compress else "off"))
        if flush_before_backup and backup_mode != "full":
            _out("  Flush first  : yes (OPTIMIZE FINAL ran before backup)")
        if note: _out("  Note         : " + note)
        if tag:  _out("  Tag          : " + tag)
        _hr()

        t0 = time.monotonic()
        try:
            _out("  Collecting table stats...")
            snaps = [self.ch.snap(t) for t in resolved]
            entry.table_snaps = [s.to_dict() for s in snaps]
            _out("  Running BACKUP command...")
            dest      = self.store.ch_dest(bid)
            base_dest = self.store.ch_dest(base_backup_id) if base_backup_id else None
            self.ch.backup_cmd(resolved, dest, self.cfg, base_dest=base_dest)
            elapsed  = time.monotonic() - t0
            size     = self.store.size(bid)
            combined = "|".join(s.checksum for s in snaps)
            entry.checksum_master  = hashlib.md5(combined.encode()).hexdigest()
            entry.status           = "ok"
            entry.size_bytes       = size
            entry.duration_seconds = elapsed
        except Exception as exc:
            entry.status           = "failed"
            entry.note            += " [ERROR: " + str(exc) + "]"
            entry.duration_seconds = time.monotonic() - t0
            self.catalog.update(entry)
            raise BackupError("Backup failed: " + str(exc)) from exc

        self.catalog.update(entry)
        _out("  Size         : " + entry.size_h)
        _out("  Duration     : " + entry.dur_h)
        _out("  Status       : OK")
        _hr()
        if self.cfg.verify: self.verify(bid)
        return entry

    def verify(self, backup_id):
        entry = self.catalog.get(backup_id)
        if not entry: raise VerifyError("Not in catalog: " + backup_id)
        _out("  Verifying " + backup_id + "...")
        errors = []
        if not self.store.exists(backup_id): errors.append("Backup not found in storage.")
        elif self.store.size(backup_id) == 0: errors.append("Backup size is 0.")
        if self.ch.ping() and entry.table_snaps:
            for sd in entry.table_snaps:
                old = TableSnap.from_dict(sd)
                try:
                    cur = self.ch.snap(old.fqn)
                    if old.checksum and cur.checksum and old.checksum != cur.checksum:
                        errors.append(old.fqn + ": checksum mismatch")
                    else:
                        _out("    " + old.fqn + ": " + str(old.row_count) + " rows  OK")
                except Exception as e: errors.append(old.fqn + ": " + str(e))
        if errors:
            for e in errors: _warn("  WARNING: " + e)
            return False
        entry.status = "verified"; self.catalog.update(entry)
        _out("  Verification passed"); return True

    def prune(self, keep_days=None, keep_count=None, dry_run=False):
        if keep_days is not None:    ids = self.catalog.prune_by_days(keep_days)
        elif keep_count is not None: ids = self.catalog.prune_by_count(keep_count)
        else: raise BackupError("Specify --keep-days or --keep-count")
        for bid in ids:
            if dry_run: _out("  [dry-run] would delete: " + bid)
            else: self.store.delete(bid); _out("  Deleted: " + bid)
        return ids


# ---------------------------------------------------------------------------
# Restorer  (RESTORE TABLE ... AS temp  +  EXCEPT INSERT)
# ---------------------------------------------------------------------------
class Restorer:
    """
    Restore using RESTORE TABLE original AS temp FROM backup.

    ClickHouse follows the full->differential/incremental chain internally.
    No missing files, no manual part copying.

    Duplicate prevention:
      INSERT INTO original SELECT * FROM temp EXCEPT SELECT * FROM original

    Mutation awareness:
      Waits for all pending DELETE/UPDATE mutations before running EXCEPT.
    """

    def __init__(self, cfg):
        self.cfg     = cfg
        self.catalog = Catalog(cfg.catalog)
        self.ch      = CH(cfg)

    def _build_dest(self, entry):
        cfg2 = Config.from_env()
        cfg2.storage         = entry.storage_backend
        cfg2.backup_dir      = self.cfg.backup_dir
        cfg2.s3_bucket       = entry.s3_bucket      or self.cfg.s3_bucket
        cfg2.s3_prefix       = entry.s3_prefix      or self.cfg.s3_prefix
        cfg2.s3_region       = entry.s3_region      or self.cfg.s3_region
        cfg2.s3_endpoint     = entry.s3_endpoint    or self.cfg.s3_endpoint
        cfg2.s3_access_key   = self.cfg.s3_access_key
        cfg2.s3_secret_key   = self.cfg.s3_secret_key
        cfg2.gcs_bucket      = entry.gcs_bucket     or self.cfg.gcs_bucket
        cfg2.gcs_credentials = self.cfg.gcs_credentials
        cfg2.azure_account   = entry.azure_account  or self.cfg.azure_account
        cfg2.azure_container = entry.azure_container or self.cfg.azure_container
        cfg2.azure_sas_token = self.cfg.azure_sas_token
        cfg2.azure_conn_str  = self.cfg.azure_conn_str
        return Storage(cfg2).ch_dest(entry.storage_path)

    def restore(self, target_time=None, backup_id=None, tables=None, dry_run=False):
        if backup_id:
            entry = self.catalog.get(backup_id)
            if not entry:
                raise RestoreError("Backup not found in catalog: " + backup_id)
        elif target_time:
            dt    = _parse_time(target_time)
            entry = self.catalog.find_best_for(dt)
            if not entry:
                raise RestoreError(
                    "No successful backup found on or before " + target_time + " UTC.\n"
                    "  Run 'list' to see available backups.")
        else:
            raise RestoreError("Specify --target-time or --backup-id")

        chain          = self.catalog.walk_chain(entry)
        restore_tables = list(tables) if tables else entry.tables
        dest           = self._build_dest(entry)

        _hr()
        _out("  PITR RESTORE")
        _out("  Method       : RESTORE TABLE ... AS temp  +  EXCEPT INSERT")
        _out("  Backup ID    : " + entry.backup_id)
        _out("  Type         : " + entry.backup_type.upper())
        _out("  Timestamp    : " + entry.timestamp[:19] + " UTC")
        if len(chain) > 1:
            _out("  Chain        :")
            for i, e in enumerate(chain):
                label = "FULL  " if e.backup_type == "full" else e.backup_type.upper()[:5] + " " + str(i)
                mark  = "  <- restoring this" if e.backup_id == entry.backup_id else ""
                _out("    [" + label + "] " + e.backup_id +
                     "  " + e.timestamp[:19] + "  " + e.size_h + mark)
        _out("  Tables       : " + ", ".join(restore_tables))
        _hr()

        if dry_run:
            self.ch.connect()
            for fqn in restore_tables:
                db, tbl = _fqn(fqn)
                rc   = self.ch.row_count(db, tbl) if self.ch.table_exists(db, tbl) else 0
                mode = "direct restore" if rc == 0 else "restore to temp -> EXCEPT INSERT -> original"
                _out("  [DRY RUN] " + fqn)
                _out("    Original rows : " + str(rc))
                _out("    Mode          : " + mode)
                _out("    Destination   : " + dest)
            _out("")
            _out("  [DRY RUN] No restore performed.")
            return entry

        self.ch.connect()
        if not self.ch.ping():
            raise RestoreError("Cannot connect to ClickHouse at " +
                               self.cfg.host + ":" + str(self.cfg.port))

        for fqn in restore_tables:
            db, tbl = _fqn(fqn)
            _out("")
            _out("  Table: " + fqn)

            if not self.ch.table_exists(db, tbl):
                _out("  Table does not exist — creating from backup DDL...")
                ts_suffix_pre = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                tmp_tbl_pre   = tbl + "_pitr_" + ts_suffix_pre
                try:
                    # Restore into a temp table first to get DDL
                    self.ch.restore_as(db, tbl, db, tmp_tbl_pre, dest)
                    # Get DDL from temp table and recreate as target
                    ddl_rows = self.ch.rows("SHOW CREATE TABLE " + db + "." + tmp_tbl_pre)
                    ddl = ddl_rows[0][0] if ddl_rows else ""
                    import re as _re
                    # Replace temp table name with target name
                    ddl = _re.sub(
                        r"CREATE TABLE\s+`?" + db + r"`?\.`?" + tmp_tbl_pre + r"`?",
                        "CREATE TABLE " + db + "." + tbl,
                        ddl, flags=_re.IGNORECASE)
                    self.ch.cmd(ddl)
                    _out("  Table created from backup DDL")
                    # Copy all rows from temp to target
                    self.ch.cmd("INSERT INTO " + db + "." + tbl +
                                " SELECT * FROM " + db + "." + tmp_tbl_pre)
                    final = self.ch.row_count(db, tbl)
                    _out("  Rows restored: " + str(final))
                    self.ch.drop_table(db, tmp_tbl_pre)
                    _out("  Done: " + fqn)
                except Exception as exc:
                    self.ch.drop_table(db, tmp_tbl_pre)
                    raise RestoreError("Auto-create restore failed: " + str(exc)) from exc
                continue

            existing_rows = self.ch.row_count(db, tbl)
            ts_suffix     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            tmp_tbl       = tbl + "_pitr_" + ts_suffix

            if self.ch.table_exists(db, tmp_tbl):
                self.ch.drop_table(db, tmp_tbl)

            _out("  Step 1/4  RESTORE " + fqn + " AS " + db + "." + tmp_tbl + "...")
            t0 = time.monotonic()
            try:
                self.ch.restore_as(db, tbl, db, tmp_tbl, dest)
            except Exception as exc:
                self.ch.drop_table(db, tmp_tbl)
                raise RestoreError("RESTORE failed: " + str(exc)) from exc

            _out("  Restored in " + _hdur(time.monotonic() - t0))
            tmp_rows = self.ch.row_count(db, tmp_tbl)
            _out("  Temp table has " + str(tmp_rows) + " rows")

            if tmp_rows == 0:
                _warn("  Temp table is empty - nothing to insert")
                self.ch.drop_table(db, tmp_tbl)
                continue

            if existing_rows == 0:
                _out("  Step 2/4  Table is empty - inserting all rows...")
                self.ch.cmd("INSERT INTO " + db + "." + tbl +
                            " SELECT * FROM " + db + "." + tmp_tbl)
                final = self.ch.row_count(db, tbl)
                _out("  Rows inserted : " + str(final))
            else:
                _out("  Step 2/4  Waiting for pending mutations...")
                self.ch.wait_mutations(db, tbl)

                _out("  Step 3/4  INSERT INTO " + db + "." + tbl +
                     " SELECT * FROM " + db + "." + tmp_tbl +
                     " EXCEPT SELECT * FROM " + db + "." + tbl + " ...")
                self.ch.insert_except(db, tbl, db, tmp_tbl)

                final = self.ch.row_count(db, tbl)
                added = final - existing_rows
                _out("  Rows before   : " + str(existing_rows))
                _out("  Rows added    : " + str(added))
                _out("  Rows after    : " + str(final))

            _out("  Step 4/4  Dropping temp table " + db + "." + tmp_tbl + " ...")
            self.ch.drop_table(db, tmp_tbl)
            _out("  Temp table dropped")

        _hr()
        _out("  Restore complete.")
        _hr()
        return entry


# ---------------------------------------------------------------------------
# AttachPartRestorer  (ATTACH PART + dependency resolution)
# ---------------------------------------------------------------------------
class AttachPartRestorer:
    """
    Restore using ATTACH PART with full dependency resolution.

    For differential/incremental backups, parts reference dictionary files
    stored in the full (base) backup. This class resolves them automatically:
      1. Parse checksums.txt to get full file list for each part
      2. Copy files present in the backup part
      3. For missing files, search all backups in chain and copy from there
      4. Generate columns.txt if missing (ClickHouse 26.x uses columns_substreams.txt)
      5. Fix ownership, then ATTACH PART

    Only works with --storage local.
    """

    def __init__(self, cfg):
        self.cfg     = cfg
        self.catalog = Catalog(cfg.catalog)
        self.ch      = CH(cfg)
        self.store   = Storage(cfg)

    def _parse_checksums(self, part_path):
        cs_file = part_path / "checksums.txt"
        if not cs_file.exists():
            return set()
        required = set()
        for line in cs_file.read_text("utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and "." in parts[0] and not parts[0].startswith("checksums"):
                required.add(parts[0])
        return required

    def _build_file_index(self, backup_root, db, tbl):
        index = {}
        table_dir = backup_root / "data" / db / tbl
        if not table_dir.exists():
            return index
        for part_dir in table_dir.iterdir():
            if not part_dir.is_dir():
                continue
            for f in part_dir.iterdir():
                if f.is_file() and f.name not in index:
                    index[f.name] = f
        return index

    def _fix_ownership(self, path):
        try:
            import pwd, grp
            uid = pwd.getpwnam("clickhouse").pw_uid
            gid = grp.getgrnam("clickhouse").gr_gid
            p = Path(path)
            if p.is_file():
                os.chown(str(p), uid, gid)
            else:
                for root, dirs, files in os.walk(str(p)):
                    os.chown(root, uid, gid)
                    for f in files:
                        os.chown(os.path.join(root, f), uid, gid)
        except Exception: pass

    def _generate_columns_txt(self, part_path, db, tbl):
        columns_txt = part_path / "columns.txt"
        if columns_txt.exists():
            return
        col_defs = self.ch.column_defs(db, tbl)
        if not col_defs:
            return
        lines = ["columns format version: 1\n", str(len(col_defs)) + " columns:\n"]
        for name, typ in col_defs:
            lines.append(name + " " + typ + "\n")
        columns_txt.write_text("".join(lines), encoding="utf-8")
        self._fix_ownership(columns_txt)

    def _is_valid_part(self, part_path):
        has_checksum = (part_path / "checksums.txt").exists()
        has_count    = (part_path / "count.txt").exists()
        has_data     = any(f.suffix == ".bin" for f in part_path.iterdir() if f.is_file())
        return has_checksum and has_count and has_data

    def _prepare_part(self, src_part, dst_detached, file_index):
        dst_part = dst_detached / src_part.name
        if dst_part.exists():
            shutil.rmtree(dst_part)
        dst_part.mkdir(parents=True)

        required = self._parse_checksums(src_part)
        if not required:
            for f in src_part.iterdir():
                if f.is_file():
                    shutil.copy2(str(f), str(dst_part / f.name))
            return dst_part

        from_chain = []
        missing    = []

        for fname in required:
            src_file = src_part / fname
            if src_file.exists():
                shutil.copy2(str(src_file), str(dst_part / fname))
            elif fname in file_index:
                shutil.copy2(str(file_index[fname]), str(dst_part / fname))
                from_chain.append(fname)
            else:
                missing.append(fname)

        cs = src_part / "checksums.txt"
        if cs.exists():
            shutil.copy2(str(cs), str(dst_part / "checksums.txt"))

        if from_chain:
            _out("    Resolved " + str(len(from_chain)) +
                 " file(s) from base backup: " +
                 ", ".join(from_chain[:3]) + ("..." if len(from_chain) > 3 else ""))
        if missing:
            _warn("    Still missing " + str(len(missing)) +
                  " file(s): " + ", ".join(missing))

        return dst_part

    def _attach_into(self, parts, chain, dst_db, dst_tbl, src_db, src_tbl, ch_data_dir):
        file_index = {}
        for entry in chain:
            idx = self._build_file_index(self.store.local_path(entry.backup_id), src_db, src_tbl)
            for fname, fpath in idx.items():
                if fname not in file_index:
                    file_index[fname] = fpath

        _out("    File index: " + str(len(file_index)) + " unique files across " +
             str(len(chain)) + " backup(s)")

        detached = Path(ch_data_dir) / "data" / dst_db / dst_tbl / "detached"
        detached.mkdir(parents=True, exist_ok=True)

        self.ch.stop_merges(dst_db, dst_tbl)
        attached, failed = [], []

        for part in parts:
            pname = part.name
            _out("    Preparing  " + pname + " ...")
            try:
                dst_part = self._prepare_part(part, detached, file_index)
                self._fix_ownership(dst_part)
                self._generate_columns_txt(dst_part, src_db, src_tbl)
                _out("    Attaching  " + pname + " ...")
                self.ch.attach_part(dst_db, dst_tbl, pname)
                attached.append(pname)
            except Exception as exc:
                _warn("    FAILED " + pname + ": " + str(exc))
                failed.append(pname)
                bad = detached / pname
                if bad.exists():
                    shutil.rmtree(bad, ignore_errors=True)

        self.ch.start_merges(dst_db, dst_tbl)
        _out("    Attached: " + str(len(attached)) + "  Failed: " + str(len(failed)))
        if failed:
            _warn("    Failed parts: " + ", ".join(failed))
        return attached, failed

    def restore(self, target_time=None, backup_id=None, tables=None,
                ch_data_dir="/var/lib/clickhouse", dry_run=False):
        if backup_id:
            entry = self.catalog.get(backup_id)
            if not entry:
                raise RestoreError("Backup not found in catalog: " + backup_id)
        elif target_time:
            dt    = _parse_time(target_time)
            entry = self.catalog.find_best_for(dt)
            if not entry:
                raise RestoreError("No successful backup found on or before " + target_time + " UTC.")
        else:
            raise RestoreError("Specify --target-time or --backup-id")

        if self.cfg.storage != "local":
            raise RestoreError("ATTACH PART restore requires --storage local.")

        backup_path = self.store.local_path(entry.backup_id)
        if not backup_path.exists():
            raise RestoreError("Backup directory not found: " + str(backup_path))

        chain          = self.catalog.walk_chain(entry)
        restore_tables = list(tables) if tables else entry.tables

        _hr()
        _out("  PITR RESTORE  (ATTACH PART + dependency resolution)")
        _out("  Backup ID    : " + entry.backup_id)
        _out("  Type         : " + entry.backup_type.upper())
        _out("  Timestamp    : " + entry.timestamp[:19] + " UTC")
        if len(chain) > 1:
            _out("  Chain        :")
            for i, e in enumerate(chain):
                label = "FULL  " if e.backup_type == "full" else e.backup_type.upper()[:5] + " " + str(i)
                mark  = "  <- attaching this" if e.backup_id == entry.backup_id else ""
                _out("    [" + label + "] " + e.backup_id +
                     "  " + e.timestamp[:19] + "  " + e.size_h + mark)
        _out("  Tables       : " + ", ".join(restore_tables))
        _out("  CH data dir  : " + ch_data_dir)
        _hr()

        if dry_run:
            self.ch.connect()
            for fqn in restore_tables:
                db, tbl = _fqn(fqn)
                table_dir = self.store.local_path(entry.backup_id) / "data" / db / tbl
                parts = ([p for p in table_dir.iterdir()
                          if p.is_dir() and self._is_valid_part(p)]
                         if table_dir.exists() else [])
                rc   = self.ch.row_count(db, tbl) if self.ch.table_exists(db, tbl) else 0
                mode = "direct attach" if rc == 0 else "temp table -> EXCEPT INSERT -> original"
                _out("  [DRY RUN] " + fqn)
                _out("    Valid parts   : " + str(len(parts)))
                _out("    Original rows : " + str(rc))
                _out("    Mode          : " + mode)
                for p in parts:
                    required = self._parse_checksums(p)
                    present  = {f.name for f in p.iterdir() if f.is_file()}
                    missing  = required - present
                    _out("      + " + p.name +
                         ("  [" + str(len(missing)) + " file(s) need resolving from base]"
                          if missing else "  [self-contained]"))
            _out("")
            _out("  [DRY RUN] No files copied, no parts attached.")
            return entry

        self.ch.connect()
        if not self.ch.ping():
            raise RestoreError("Cannot connect to ClickHouse at " +
                               self.cfg.host + ":" + str(self.cfg.port))

        for fqn in restore_tables:
            db, tbl = _fqn(fqn)
            _out("")
            _out("  Table: " + fqn)

            table_dir = self.store.local_path(entry.backup_id) / "data" / db / tbl
            if not table_dir.exists():
                _warn("  No data directory found - skipping")
                continue

            parts = sorted([p for p in table_dir.iterdir()
                            if p.is_dir() and self._is_valid_part(p)])
            if not parts:
                _warn("  No valid parts found - skipping")
                continue

            _out("  Found " + str(len(parts)) + " valid part(s)")

            if not self.ch.table_exists(db, tbl):
                _out("  Table does not exist — attempting to create from backup metadata...")
                # Try to get DDL from backup using RESTORE AS into a temp table
                ts_pre = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                tmp_pre = tbl + "_pitr_ddl_" + ts_pre
                try:
                    self.ch.restore_as(db, tbl, db, tmp_pre, dest)
                    ddl_rows = self.ch.rows("SHOW CREATE TABLE " + db + "." + tmp_pre)
                    ddl = ddl_rows[0][0] if ddl_rows else ""
                    import re as _re2
                    ddl = _re2.sub(
                        r"CREATE TABLE\s+`?" + db + r"`?\.`?" + tmp_pre + r"`?",
                        "CREATE TABLE " + db + "." + tbl,
                        ddl, flags=_re2.IGNORECASE)
                    self.ch.cmd(ddl)
                    _out("  Table created from backup DDL")
                    self.ch.drop_table(db, tmp_pre)
                except Exception as exc:
                    self.ch.drop_table(db, tmp_pre)
                    raise RestoreError("Cannot create table automatically: " + str(exc) +
                                       "\nCreate the table manually first, then restore.") from exc

            existing_rows = self.ch.row_count(db, tbl)

            if existing_rows == 0:
                _out("  Mode: DIRECT ATTACH (table is empty)")
                self._attach_into(parts, chain, db, tbl, db, tbl, ch_data_dir)
                _out("  Row count after attach: " + str(self.ch.row_count(db, tbl)))
            else:
                ts_suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                tmp_tbl   = tbl + "_pitr_attach_" + ts_suffix

                _out("  Mode: TEMP TABLE -> EXCEPT INSERT -> ORIGINAL")
                _out("  Original has " + str(existing_rows) + " rows")
                _out("  Temp table   : " + db + "." + tmp_tbl)
                _out("")

                _out("  Step 1/4  Creating " + db + "." + tmp_tbl + "...")
                self.ch.cmd("CREATE TABLE " + db + "." + tmp_tbl + " AS " + db + "." + tbl)

                _out("  Step 2/4  Attaching parts into temp table...")
                self._attach_into(parts, chain, db, tmp_tbl, db, tbl, ch_data_dir)

                tmp_rows = self.ch.row_count(db, tmp_tbl)
                _out("  Temp table has " + str(tmp_rows) + " rows after attach")

                if tmp_rows == 0:
                    _warn("  Temp table is empty - nothing to insert")
                    self.ch.drop_table(db, tmp_tbl)
                    continue

                _out("  Step 3/4  Waiting for pending mutations...")
                self.ch.wait_mutations(db, tbl)
                _out("  Step 3/4  INSERT EXCEPT ...")
                self.ch.insert_except(db, tbl, db, tmp_tbl)

                final = self.ch.row_count(db, tbl)
                added = final - existing_rows
                _out("  Rows before  : " + str(existing_rows))
                _out("  Rows added   : " + str(added))
                _out("  Rows after   : " + str(final))

                _out("  Step 4/4  Dropping temp table...")
                self.ch.drop_table(db, tmp_tbl)
                _out("  Temp table dropped")

        _hr()
        _out("  Restore complete.")
        _hr()
        return entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fqn(fqn):
    p = fqn.split(".", 1)
    return (p[0], p[1]) if len(p) == 2 else ("default", p[0])

def _now_iso(): return datetime.now(timezone.utc).isoformat()

def _parse_time(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try: return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError: pass
    raise ValueError("Cannot parse time: '" + s + "'  (expected: YYYY-MM-DD HH:MM:SS)")

def _hbytes(n):
    for u in ("B","KB","MB","GB","TB"):
        if n < 1024: return str(round(n, 1)) + " " + u
        n /= 1024
    return str(round(n, 1)) + " PB"

def _hdur(s):
    s = int(s); h, r = divmod(s, 3600); m, sc = divmod(r, 60)
    if h: return str(h) + "h " + str(m) + "m " + str(sc) + "s"
    if m: return str(m) + "m " + str(sc) + "s"
    return str(sc) + "s"

def _out(msg):
    if RICH: console.print(msg)
    else:    print(msg)

def _warn(msg):
    if RICH: console.print("[yellow]" + msg + "[/yellow]")
    else:    print("WARNING: " + msg, file=sys.stderr)

def _hr():
    if RICH: console.rule(style="dim")
    else:    print("-" * 70)

def _retry(fn, max_retries, delay):
    for attempt in range(1, max_retries + 1):
        try: return fn()
        except Exception as exc:
            if attempt == max_retries: raise
            _warn("Attempt " + str(attempt) + "/" + str(max_retries) +
                  " failed: " + str(exc) + "  (retry in " + str(delay) + "s...)")
            time.sleep(delay)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    desc = (
        "ClickHouse Point-in-Time Recovery Tool  v" + VERSION + "\n\n"
        "BACKUP TYPES:\n"
        "  full           Complete snapshot\n"
        "  differential   Changes since latest FULL backup\n"
        "                 Restore needs: full + this differential only\n"
        "  incremental    Changes since latest backup of ANY type\n"
        "                 Restore needs: full + all incrementals in chain\n\n"
        "RESTORE METHODS:\n"
        "  restore (default)  RESTORE TABLE ... AS temp + EXCEPT INSERT\n"
        "                     Works with all storage backends\n"
        "  attach (experimental)  ATTACH PART with dependency resolution\n"
        "                         Local storage only\n\n"
        "STORAGE BACKENDS:\n"
        "  local   Local disk or NFS\n"
        "  s3      AWS S3, MinIO, Ceph RGW\n"
        "  gcs     Google Cloud Storage\n"
        "  azure   Azure Blob Storage\n"
    )
    parser = argparse.ArgumentParser(
        prog="clickhouse_pitr",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=desc)
    parser.add_argument("-v","--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument("--version", action="version", version="clickhouse_pitr " + VERSION)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ── backup ────────────────────────────────────────────────────────────────
    bp = sub.add_parser("backup", help="Take a full, differential, or incremental backup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Take a backup.\n"
            "ClickHouse 22.7+ for full, 23.x+ for differential/incremental.\n\n"
            "BACKUP TYPE (choose one):\n"
            "  (none)           Full backup  (default)\n"
            "  --differential   Changes since latest full backup\n"
            "                   Base: always the latest full backup\n"
            "                   Chain: Full -> Diff(base:Full) -> Diff(base:Full)\n"
            "  --incremental    Changes since latest backup of any type\n"
            "                   Base: latest backup (full or incremental)\n"
            "                   Chain: Full -> Incr1(base:Full) -> Incr2(base:Incr1)\n\n"
            "TABLE SELECTION (choose one):\n"
            "  --all-tables           All non-system tables\n"
            "  --database DB          All tables in one database\n"
            "  --tables db.t1 db.t2   Specific tables\n\n"
            "EXAMPLES:\n"
            "  # Full backup\n"
            "  python3 clickhouse_pitr.py backup \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --tables uk.uk_price_paid\n\n"
            "  # Differential backup (base: latest full)\n"
            "  python3 clickhouse_pitr.py backup \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --tables uk.uk_price_paid --differential --flush-before-backup\n\n"
            "  # Incremental backup (base: latest backup of any type)\n"
            "  python3 clickhouse_pitr.py backup \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --tables uk.uk_price_paid --incremental --flush-before-backup\n\n"
            "  # S3 backup\n"
            "  python3 clickhouse_pitr.py backup \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage s3 --s3-bucket my-bucket --s3-region eu-west-1 \\\n"
            "      --s3-access-key AKIA... --s3-secret-key ... \\\n"
            "      --tables uk.uk_price_paid\n\n"
            "  # MinIO backup\n"
            "  python3 clickhouse_pitr.py backup \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage s3 --s3-bucket backups \\\n"
            "      --s3-endpoint http://minio:9000 \\\n"
            "      --s3-access-key minioadmin --s3-secret-key minioadmin \\\n"
            "      --s3-path-style --tables uk.uk_price_paid\n"
        ))

    bg = bp.add_argument_group("ClickHouse connection")
    bg.add_argument("--host",     default=None, metavar="HOST", help="Host (CH_HOST)")
    bg.add_argument("--port",     default=None, metavar="PORT", type=int, help="HTTP port (CH_PORT)")
    bg.add_argument("--username", default=None, metavar="USER", help="Username (CH_USER)")
    bg.add_argument("--password", default=None, metavar="PASS", help="Password (CH_PASSWORD)")

    tg = bp.add_argument_group("Table selection  [choose one]")
    tg.add_argument("--all-tables", action="store_true",              help="All non-system tables")
    tg.add_argument("--database",   default=None, metavar="DB",       help="All tables in this database")
    tg.add_argument("--tables",     default=None, metavar="DB.TABLE", nargs="+", help="Specific tables")

    sg = bp.add_argument_group("Storage backend")
    sg.add_argument("--storage",      default=None, metavar="BACKEND", choices=["local","s3","gcs","azure"],
                    help="local | s3 | gcs | azure  (default: local / CH_STORAGE)")
    sg.add_argument("--backup-dir",   default=None, metavar="DIR",   help="Local backup dir (CH_BACKUP_DIR)")
    sg.add_argument("--catalog-file", default=None, metavar="FILE",  help="Catalog JSON (CH_CATALOG_FILE)")

    s3g = bp.add_argument_group("S3 / MinIO / Ceph  [--storage s3]")
    s3g.add_argument("--s3-bucket",     default=None, metavar="BUCKET")
    s3g.add_argument("--s3-prefix",     default=None, metavar="PREFIX")
    s3g.add_argument("--s3-region",     default=None, metavar="REGION")
    s3g.add_argument("--s3-endpoint",   default=None, metavar="URL")
    s3g.add_argument("--s3-access-key", default=None, metavar="KEY")
    s3g.add_argument("--s3-secret-key", default=None, metavar="SECRET")
    s3g.add_argument("--s3-path-style", action="store_true")

    gcsg = bp.add_argument_group("Google Cloud Storage  [--storage gcs]")
    gcsg.add_argument("--gcs-bucket",      default=None, metavar="BUCKET")
    gcsg.add_argument("--gcs-credentials", default=None, metavar="FILE")

    azg = bp.add_argument_group("Azure Blob Storage  [--storage azure]")
    azg.add_argument("--azure-account",   default=None, metavar="ACCOUNT")
    azg.add_argument("--azure-container", default=None, metavar="CONTAINER")
    azg.add_argument("--azure-sas-token", default=None, metavar="TOKEN")
    azg.add_argument("--azure-conn-str",  default=None, metavar="CONNSTR")

    og = bp.add_argument_group("Backup options")
    og.add_argument("--tag",              default="",  metavar="TAG", help="Append tag to backup ID")
    og.add_argument("--note",             default="",  metavar="TXT", help="Free-text note in catalog")
    og.add_argument("--no-compress",      action="store_true",        help="Disable LZ4 compression")
    og.add_argument("--parallel-threads", default=None, metavar="N",  type=int)
    og.add_argument("--no-verify",        action="store_true",        help="Skip post-backup verification")
    og.add_argument("--max-retries",      default=None, metavar="N",  type=int)

    btg = bp.add_argument_group("Backup type  [choose one, default: full]")
    bex = btg.add_mutually_exclusive_group()
    bex.add_argument("--differential", action="store_true",
                     help=(
                         "Differential backup: captures all changes since the latest FULL backup.\n"
                         "Base is always the latest full backup.\n"
                         "Restore needs: full + this differential only.\n"
                         "Simple and reliable. Recommended for most use cases."
                     ))
    bex.add_argument("--incremental", action="store_true",
                     help=(
                         "Incremental backup: captures changes since the latest backup of ANY type.\n"
                         "Base is the most recent backup (full or incremental).\n"
                         "Restore needs: full + all incrementals in chain.\n"
                         "Smaller backups, but restore chain can grow long."
                     ))
    btg.add_argument("--base-backup-id", default="", metavar="ID",
                     help="Override auto-detected base backup ID")
    btg.add_argument("--flush-before-backup", action="store_true",
                     help=(
                         "Run OPTIMIZE TABLE FINAL before backup.\n"
                         "Ensures all recent INSERTs are captured (prevents empty differential/incremental).\n"
                         "Recommended when taking differential or incremental backups."
                     ))

    # ── restore ───────────────────────────────────────────────────────────────
    rp = sub.add_parser("restore", help="Restore a backup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Restore a backup. Two methods available:\n\n"
            "METHOD: restore (default)\n"
            "  RESTORE TABLE original AS temp FROM backup\n"
            "  ClickHouse follows full->differential/incremental chain internally.\n"
            "  Then: wait mutations -> INSERT EXCEPT -> DROP temp\n"
            "  Works with all storage backends.\n\n"
            "METHOD: attach (experimental, local only)\n"
            "  ATTACH PART with dependency resolution.\n"
            "  Reads checksums.txt, finds missing files from base backup,\n"
            "  copies them before attaching.\n\n"
            "BOTH METHODS:\n"
            "  Never delete rows from the original table.\n"
            "  INSERT EXCEPT prevents any duplicate rows.\n"
            "  Wait for mutations before EXCEPT (DELETE awareness).\n\n"
            "EXAMPLES:\n"
            "  # Dry-run first\n"
            "  python3 clickhouse_pitr.py restore \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --backup-id backup_20260331_112416 --dry-run\n\n"
            "  # Restore (default method)\n"
            "  python3 clickhouse_pitr.py restore \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --backup-id backup_20260331_112416\n\n"
            "  # Restore using ATTACH PART (experimental)\n"
            "  python3 clickhouse_pitr.py restore \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --backup-id backup_20260331_112416 --method attach\n\n"
            "  # Restore by point in time\n"
            "  python3 clickhouse_pitr.py restore \\\n"
            "      --host localhost --port 8123 \\\n"
            "      --username default --password default \\\n"
            "      --storage local --backup-dir /var/lib/clickhouse/backups \\\n"
            "      --target-time \"2026-03-31 11:24:16\"\n"
        ))

    rbg = rp.add_argument_group("ClickHouse connection")
    rbg.add_argument("--host",     default=None, metavar="HOST")
    rbg.add_argument("--port",     default=None, metavar="PORT", type=int)
    rbg.add_argument("--username", default=None, metavar="USER")
    rbg.add_argument("--password", default=None, metavar="PASS")
    rbg.add_argument("--database", default=None, metavar="DB")

    rtg = rp.add_argument_group("Restore target")
    rtg.add_argument("--target-time", default=None, metavar="DATETIME",
                     help="Target UTC time: 'YYYY-MM-DD HH:MM:SS'")
    rtg.add_argument("--backup-id",   default=None, metavar="ID")
    rtg.add_argument("--tables",      default=None, metavar="DB.TABLE", nargs="+")
    rtg.add_argument("--dry-run",     action="store_true",
                     help="Print plan only, do not restore")
    rtg.add_argument("--method",      default="restore",
                     choices=["restore","attach"],
                     help="restore (default) or attach (experimental, local only)")

    rsg = rp.add_argument_group("Storage / paths")
    rsg.add_argument("--storage",      default=None, metavar="BACKEND", choices=["local","s3","gcs","azure"])
    rsg.add_argument("--backup-dir",   default=None, metavar="DIR")
    rsg.add_argument("--catalog-file", default=None, metavar="FILE")
    rsg.add_argument("--ch-data-dir",  default="/var/lib/clickhouse", metavar="DIR",
                     help="ClickHouse data directory (required for --method attach)")
    rsg.add_argument("--s3-bucket",    default=None, metavar="BUCKET")
    rsg.add_argument("--s3-prefix",    default=None, metavar="PREFIX")
    rsg.add_argument("--s3-region",    default=None, metavar="REGION")
    rsg.add_argument("--s3-endpoint",  default=None, metavar="URL")
    rsg.add_argument("--s3-access-key",default=None, metavar="KEY")
    rsg.add_argument("--s3-secret-key",default=None, metavar="SECRET")
    rsg.add_argument("--gcs-bucket",     default=None, metavar="BUCKET")
    rsg.add_argument("--gcs-credentials",default=None, metavar="FILE")
    rsg.add_argument("--azure-account",  default=None, metavar="ACCOUNT")
    rsg.add_argument("--azure-container",default=None, metavar="CONTAINER")
    rsg.add_argument("--azure-sas-token",default=None, metavar="TOKEN")
    rsg.add_argument("--azure-conn-str", default=None, metavar="CONNSTR")

    # ── list ──────────────────────────────────────────────────────────────────
    lp = sub.add_parser("list", help="List all backups",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List all backups.\n\nEXAMPLES:\n  python3 clickhouse_pitr.py list\n  python3 clickhouse_pitr.py list --status verified\n  python3 clickhouse_pitr.py list --json\n")
    lp.add_argument("--catalog-file", default=None, metavar="FILE")
    lp.add_argument("--status", default=None,
                    choices=["ok","running","failed","partial","verified"])
    lp.add_argument("--limit", default=50, metavar="N", type=int)
    lp.add_argument("--json", action="store_true")

    # ── info ──────────────────────────────────────────────────────────────────
    ip = sub.add_parser("info", help="Show full details of one backup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Show all metadata for one backup.\n\nEXAMPLES:\n  python3 clickhouse_pitr.py info --backup-id backup_20260331_112416\n")
    ip.add_argument("--catalog-file", default=None, metavar="FILE")
    ip.add_argument("--backup-id",    required=True, metavar="ID")
    ip.add_argument("--json",         action="store_true")

    # ── verify ────────────────────────────────────────────────────────────────
    vp = sub.add_parser("verify", help="Verify backup integrity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Check backup exists and checksums match.\n\nEXAMPLES:\n  python3 clickhouse_pitr.py verify --backup-id backup_20260331_112416\n  python3 clickhouse_pitr.py verify --all\n")
    vp.add_argument("--host",         default=None, metavar="HOST")
    vp.add_argument("--username",     default=None, metavar="USER")
    vp.add_argument("--password",     default=None, metavar="PASS")
    vp.add_argument("--storage",      default=None, choices=["local","s3","gcs","azure"])
    vp.add_argument("--backup-dir",   default=None, metavar="DIR")
    vp.add_argument("--catalog-file", default=None, metavar="FILE")
    vp.add_argument("--s3-bucket",    default=None, metavar="BUCKET")
    vp.add_argument("--s3-prefix",    default=None, metavar="PREFIX")
    vp.add_argument("--s3-region",    default=None, metavar="REGION")
    vp.add_argument("--s3-endpoint",  default=None, metavar="URL")
    vp.add_argument("--s3-access-key",default=None, metavar="KEY")
    vp.add_argument("--s3-secret-key",default=None, metavar="SECRET")
    vp.add_argument("--backup-id",    default=None, metavar="ID")
    vp.add_argument("--all",          action="store_true")

    # ── prune ─────────────────────────────────────────────────────────────────
    pp = sub.add_parser("prune", help="Delete old backups",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Delete old backups. Use --dry-run first.\n\nEXAMPLES:\n  python3 clickhouse_pitr.py prune --keep-days 30 --dry-run\n  python3 clickhouse_pitr.py prune --keep-days 30\n  python3 clickhouse_pitr.py prune --keep-count 10\n")
    pp.add_argument("--catalog-file", default=None, metavar="FILE")
    pp.add_argument("--backup-dir",   default=None, metavar="DIR")
    pp.add_argument("--storage",      default=None, choices=["local","s3","gcs","azure"])
    pp.add_argument("--s3-bucket",    default=None, metavar="BUCKET")
    pp.add_argument("--s3-prefix",    default=None, metavar="PREFIX")
    pp.add_argument("--s3-region",    default=None, metavar="REGION")
    pp.add_argument("--s3-endpoint",  default=None, metavar="URL")
    pp.add_argument("--s3-access-key",default=None, metavar="KEY")
    pp.add_argument("--s3-secret-key",default=None, metavar="SECRET")
    pp.add_argument("--keep-days",    default=None, metavar="N", type=int)
    pp.add_argument("--keep-count",   default=None, metavar="N", type=int)
    pp.add_argument("--dry-run",      action="store_true")

    # ── chain ─────────────────────────────────────────────────────────────────
    cp = sub.add_parser("chain", help="Show backup chain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Show the backup chain.\n\nEXAMPLES:\n  python3 clickhouse_pitr.py chain --backup-id backup_20260331_112416\n  python3 clickhouse_pitr.py chain --target-time \"2026-03-31 11:24:16\"\n")
    cp.add_argument("--catalog-file", default=None, metavar="FILE")
    cp.add_argument("--target-time",  default=None, metavar="DATETIME")
    cp.add_argument("--backup-id",    default=None, metavar="ID")

    # ── schedule ──────────────────────────────────────────────────────────────
    sp = sub.add_parser("schedule", help="Generate a crontab line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Print a ready-to-paste crontab line.\n\nEXAMPLES:\n  python3 clickhouse_pitr.py schedule \\\n      --cron \"0 2 * * *\" \\\n      --storage local --backup-dir /var/lib/clickhouse/backups \\\n      --all-tables --tag nightly\n")
    sp.add_argument("--cron",         required=True, metavar="EXPR")
    sp.add_argument("--storage",      default=None, choices=["local","s3","gcs","azure"])
    sp.add_argument("--backup-dir",   default=None, metavar="DIR")
    sp.add_argument("--catalog-file", default=None, metavar="FILE")
    sp.add_argument("--s3-bucket",    default=None, metavar="BUCKET")
    sp.add_argument("--s3-prefix",    default=None, metavar="PREFIX")
    sp.add_argument("--s3-region",    default=None, metavar="REGION")
    sp.add_argument("--host",         default=None, metavar="HOST")
    sp.add_argument("--username",     default=None, metavar="USER")
    sp.add_argument("--all-tables",   action="store_true")
    sp.add_argument("--database",     default=None, metavar="DB")
    sp.add_argument("--tables",       default=None, metavar="DB.TABLE", nargs="+")
    sp.add_argument("--tag",          default="",   metavar="TAG")
    sp.add_argument("--differential", action="store_true")
    sp.add_argument("--incremental",  action="store_true")

    # ── config ────────────────────────────────────────────────────────────────
    sub.add_parser("config", help="Show current effective configuration")

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def cmd_backup(args, cfg):
    if getattr(args, "max_retries", None):
        cfg.max_retries = args.max_retries

    # Determine backup mode
    if getattr(args, "differential", False):
        backup_mode = "differential"
    elif getattr(args, "incremental", False):
        backup_mode = "incremental"
    else:
        backup_mode = "full"

    mgr = BackupManager(cfg)
    def _do():
        return mgr.take(
            tables              = getattr(args, "tables",              None),
            all_tables          = getattr(args, "all_tables",          False),
            database            = getattr(args, "database",            None),
            note                = getattr(args, "note",                ""),
            tag                 = getattr(args, "tag",                 ""),
            backup_mode         = backup_mode,
            base_backup_id      = getattr(args, "base_backup_id",      ""),
            flush_before_backup = getattr(args, "flush_before_backup", False),
        )
    _retry(_do, cfg.max_retries, cfg.retry_delay)


def cmd_restore(args, cfg):
    method = getattr(args, "method", "restore")

    if method == "attach":
        r = AttachPartRestorer(cfg)
        r.restore(
            target_time = getattr(args, "target_time", None),
            backup_id   = getattr(args, "backup_id",   None),
            tables      = getattr(args, "tables",      None),
            ch_data_dir = getattr(args, "ch_data_dir", "/var/lib/clickhouse"),
            dry_run     = getattr(args, "dry_run",     False),
        )
    else:
        r = Restorer(cfg)
        r.restore(
            target_time = getattr(args, "target_time", None),
            backup_id   = getattr(args, "backup_id",   None),
            tables      = getattr(args, "tables",      None),
            dry_run     = getattr(args, "dry_run",     False),
        )


def cmd_list(args, cfg):
    catalog = Catalog(cfg.catalog)
    entries = catalog.list_all(status=getattr(args,"status",None))[:getattr(args,"limit",50)]
    if getattr(args,"json",False):
        print(json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False)); return
    if not entries: _out("No backups found in catalog."); return
    if RICH:
        t = Table(box=rbox.SIMPLE_HEAVY, show_header=True, header_style="bold")
        t.add_column("Backup ID",       style="cyan",  no_wrap=True)
        t.add_column("Timestamp (UTC)", style="white", no_wrap=True)
        t.add_column("Type",            style="blue")
        t.add_column("Backend",         style="magenta")
        t.add_column("Status",          style="white")
        t.add_column("Size",            justify="right")
        t.add_column("Duration",        justify="right")
        t.add_column("Tables",          style="white")
        sm = {"ok":"[green]ok[/green]","verified":"[bright_green]verified[/bright_green]",
              "running":"[yellow]running[/yellow]","failed":"[red]failed[/red]",
              "partial":"[orange1]partial[/orange1]"}
        for e in entries:
            t.add_row(e.backup_id, e.timestamp[:19], e.backup_type, e.storage_backend,
                      sm.get(e.status,e.status), e.size_h, e.dur_h,
                      ", ".join(e.tables[:2]) + ("..." if len(e.tables)>2 else ""))
        console.print(t)
    else:
        fmt = "{:<36}  {:<20}  {:<14}  {:<6}  {:<9}  {:>8}  {}"
        print(fmt.format("Backup ID","Timestamp (UTC)","Type","Backend","Status","Size","Tables"))
        print("-"*115)
        for e in entries:
            print(fmt.format(e.backup_id, e.timestamp[:19], e.backup_type,
                             e.storage_backend, e.status, e.size_h, ", ".join(e.tables[:2])))


def cmd_info(args, cfg):
    catalog = Catalog(cfg.catalog)
    entry   = catalog.get(args.backup_id)
    if not entry: _warn("Not found: " + args.backup_id); sys.exit(1)
    if getattr(args,"json",False):
        print(json.dumps(entry.to_dict(), indent=2, ensure_ascii=False)); return
    rows = [
        ("Backup ID",       entry.backup_id), ("Timestamp (UTC)", entry.timestamp),
        ("Type",            entry.backup_type), ("Base backup", entry.base_backup_id or "-"),
        ("Status",          entry.status), ("Storage backend", entry.storage_backend),
        ("Storage path",    entry.storage_path), ("Size", entry.size_h), ("Duration", entry.dur_h),
        ("ClickHouse ver.", entry.ch_version), ("Note", entry.note or "-"), ("Tag", entry.tag or "-"),
        ("Checksum master", entry.checksum_master or "-"),
    ]
    if entry.s3_bucket:
        rows += [("S3 bucket",entry.s3_bucket),("S3 prefix",entry.s3_prefix),
                 ("S3 region",entry.s3_region),("S3 endpoint",entry.s3_endpoint or "(AWS standard)")]
    if entry.gcs_bucket: rows.append(("GCS bucket", entry.gcs_bucket))
    if entry.azure_container:
        rows += [("Azure account",entry.azure_account),("Azure container",entry.azure_container)]
    rows.append(("Tables","\n" + "\n".join("  - " + t for t in entry.tables)))
    if entry.table_snaps:
        lines = []
        for sd in entry.table_snaps:
            s = TableSnap.from_dict(sd)
            lines.append("  " + s.fqn.ljust(40) + str(s.row_count).rjust(14) + " rows  " +
                         _hbytes(s.size_bytes).rjust(10) + "  cs=" + s.checksum[:12] + "...")
        rows.append(("Table snapshots","\n" + "\n".join(lines)))
    _hr()
    for k, v in rows: _out("  " + k.ljust(20) + ": " + str(v))
    _hr()


def cmd_verify(args, cfg):
    mgr = BackupManager(cfg)
    if getattr(args,"all",False):
        catalog = Catalog(cfg.catalog)
        entries = catalog.list_all("ok") + catalog.list_all("verified")
        if not entries: _out("No backups to verify."); return
        results = [(e.backup_id, mgr.verify(e.backup_id)) for e in entries]
        passed  = sum(1 for _, ok in results if ok)
        _hr(); _out("  Total: " + str(len(results)) + "  Passed: " + str(passed) +
                    "  Failed: " + str(len(results)-passed))
    else:
        bid = getattr(args,"backup_id",None)
        if not bid: _warn("Specify --backup-id or --all"); sys.exit(1)
        sys.exit(0 if mgr.verify(bid) else 1)


def cmd_prune(args, cfg):
    mgr     = BackupManager(cfg)
    removed = mgr.prune(keep_days=getattr(args,"keep_days",None),
                        keep_count=getattr(args,"keep_count",None),
                        dry_run=getattr(args,"dry_run",False))
    action = "[dry-run] would delete" if getattr(args,"dry_run",False) else "Deleted"
    _out("  " + action + ": " + str(len(removed)) + " backup(s)")


def cmd_chain(args, cfg):
    catalog = Catalog(cfg.catalog); chain = []
    if getattr(args,"target_time",None):
        chain = catalog.find_chain_for(_parse_time(args.target_time))
        if not chain: _warn("No chain found for: " + args.target_time); return
    elif getattr(args,"backup_id",None):
        entry = catalog.get(args.backup_id)
        if not entry: _warn("Not found: " + args.backup_id); return
        chain = catalog.walk_chain(entry)
    else:
        _warn("Specify --target-time or --backup-id"); return
    _hr(); _out("  Backup chain  (" + str(len(chain)) + " step(s))"); _hr()
    for i, e in enumerate(chain):
        if e.backup_type == "full":
            label = "FULL         "
        elif e.backup_type == "differential":
            label = "DIFFERENTIAL " + str(i)
        else:
            label = "INCREMENTAL  " + str(i)
        _out("  [" + label + "]  " + e.backup_id)
        _out("               Time   : " + e.timestamp[:19] + " UTC")
        _out("               Size   : " + e.size_h + "  |  Status: " + e.status)
        if e.base_backup_id: _out("               Base   : " + e.base_backup_id)
    _hr(); _out("  -> RESTORING FROM: " + chain[-1].backup_id); _hr()


def cmd_schedule(args, cfg):
    script = str(Path(__file__).resolve()); python = sys.executable
    parts  = [python, script, "backup"]
    st = cfg.storage; parts += ["--storage", st]
    if st == "local": parts += ["--backup-dir", cfg.backup_dir]
    elif st == "s3":
        parts += ["--s3-bucket",cfg.s3_bucket,"--s3-prefix",cfg.s3_prefix,"--s3-region",cfg.s3_region]
        if cfg.s3_endpoint: parts += ["--s3-endpoint", cfg.s3_endpoint]
    elif st == "gcs": parts += ["--gcs-bucket", cfg.gcs_bucket]
    elif st == "azure": parts += ["--azure-account",cfg.azure_account,"--azure-container",cfg.azure_container]
    parts += ["--host", cfg.host, "--catalog-file", cfg.catalog]
    if getattr(args,"all_tables",False): parts.append("--all-tables")
    elif getattr(args,"database",None):  parts += ["--database", args.database]
    elif getattr(args,"tables",None):    parts += ["--tables"] + args.tables
    if getattr(args,"tag",""):           parts += ["--tag", args.tag]
    if getattr(args,"differential",False): parts.append("--differential")
    elif getattr(args,"incremental",False): parts.append("--incremental")
    _hr(); _out("  Add to crontab  (crontab -e):"); _hr()
    _out(args.cron + "  " + " ".join(parts)); _hr()
    _out("  Tip: append  >> /var/log/ch_pitr.log 2>&1  to capture output.")


def cmd_config(cfg):
    _hr()
    _out("  ClickHouse     : " + cfg.host + ":" + str(cfg.port))
    _out("  User           : " + cfg.user)
    _out("  Database       : " + cfg.database)
    _out("  Storage        : " + cfg.storage_label())
    _out("  Catalog        : " + str(Path(cfg.catalog).resolve()))
    _out("  Compression    : " + ("LZ4" if cfg.compress else "off"))
    _out("  Verify backup  : " + ("yes" if cfg.verify else "no"))
    _out("  Max retries    : " + str(cfg.max_retries))
    _hr()
    _out("  Packages:")
    _out("    clickhouse-connect  : " + ("yes" if CH_OK    else "NO -> pip install clickhouse-connect"))
    _out("    rich                : " + ("yes" if RICH     else "no (optional)"))
    _out("    boto3 (S3)          : " + ("yes" if BOTO3_OK else "no -> pip install boto3"))
    _out("    google-cloud-storage: " + ("yes" if GCS_OK   else "no -> pip install google-cloud-storage"))
    _out("    azure-storage-blob  : " + ("yes" if AZURE_OK else "no -> pip install azure-storage-blob"))
    _hr()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _setup_logging(verbose):
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        level=logging.DEBUG if verbose else logging.WARNING,
        stream=sys.stdout)

def _sigint(sig, frame):
    print("\nInterrupted.", file=sys.stderr); sys.exit(130)


def main():
    signal.signal(signal.SIGINT, _sigint)
    parser = build_parser()
    args   = parser.parse_args()
    _setup_logging(getattr(args,"verbose",False))
    cfg = Config.from_env()
    cfg.apply(args)
    try:
        if   args.command == "backup":   cfg.validate(); cmd_backup(args, cfg)
        elif args.command == "restore":  cfg.validate(); cmd_restore(args, cfg)
        elif args.command == "list":     cmd_list(args, cfg)
        elif args.command == "info":     cmd_info(args, cfg)
        elif args.command == "verify":   cfg.validate(); cmd_verify(args, cfg)
        elif args.command == "prune":    cmd_prune(args, cfg)
        elif args.command == "chain":    cmd_chain(args, cfg)
        elif args.command == "schedule": cmd_schedule(args, cfg)
        elif args.command == "config":   cmd_config(cfg)
    except PITRError as exc:
        _warn("Error: " + str(exc)); sys.exit(1)
    except Exception as exc:
        _warn("Unexpected error: " + str(exc))
        if getattr(args,"verbose",False): traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()