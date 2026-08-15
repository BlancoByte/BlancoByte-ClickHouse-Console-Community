#!/usr/bin/env python3
"""Re-encrypt the credential vault under the current primary master key.

Zero-downtime rotation procedure
--------------------------------
  1. Provision the NEW key as primary and the OLD key as secondary in your
     provider, so both decrypt:
       env provider :  MASTER_KEY=<new>   MASTER_KEY_SECONDARY=<old>
       file provider:  write <new> to data/global/master.key,
                       write <old> to data/global/master.key.prev
       vault provider:  set `current`=<new>, `previous`=<old> at the secret path
  2. Run this script (optionally with --dry-run first). It decrypts every stored
     credential with whichever key works and re-encrypts it under the new
     primary.
  3. Restart the application so every worker loads the new key set. (The CSRF
     token derives from the master key, so existing CSRF cookies refresh
     automatically on the next page load; in-flight writes may need one retry.)
  4. Remove the OLD secondary/previous key from the provider and run again with
     --verify to confirm everything still opens under the primary alone.

Two columns are encrypted with the master key and therefore rotated here:
user_credentials.ch_password_enc (per-user ClickHouse passwords) and
ldap_config.bind_password (the LDAP service-account secret, single row).
"""

import os
import sys
import argparse

import psycopg
from cryptography.fernet import Fernet, MultiFernet

import key_provider


def _dsn():
    return (
        "host={h} port={p} user={u} password={pw} dbname={n}".format(
            h=os.environ.get("DB_HOST", "127.0.0.1"),
            p=os.environ.get("DB_PORT", "5432"),
            u=os.environ.get("DB_USER", "postgres"),
            pw=os.environ.get("DB_PASSWORD", ""),
            n=os.environ.get("DB_NAME", "clickhouse_console"),
        )
    )


def main():
    ap = argparse.ArgumentParser(description="Rotate the credential-vault master key.")
    ap.add_argument("--global-dir", default=os.environ.get("MASTER_KEY_DIR", "data/global"),
                    help="directory holding master.key for the file provider")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    ap.add_argument("--verify", action="store_true",
                    help="only check that every token decrypts under the loaded keys")
    args = ap.parse_args()

    keys = key_provider.load_key_provider(args.global_dir).get_keys()
    if not keys:
        print("No master keys loaded \u2014 check your provider configuration.")
        sys.exit(2)
    mf = MultiFernet([Fernet(k) for k in keys])
    print("Loaded %d key(s); primary first." % len(keys))

    conn = psycopg.connect(_dsn())
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, connection_id, ch_password_enc FROM user_credentials")
        rows = cur.fetchall()
    print("%d credential row(s) to process." % len(rows))

    ok = 0
    rotated = 0
    failed = []
    for uid, cid, tok in rows:
        tok_b = tok.encode() if isinstance(tok, str) else tok
        try:
            mf.decrypt(tok_b)  # opens with whichever loaded key matches
            ok += 1
        except Exception as e:
            failed.append((uid, cid, str(e)[:120]))
            continue
        if args.verify:
            continue
        new_tok = mf.rotate(tok_b).decode()  # re-encrypt under the primary key
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE user_credentials SET ch_password_enc=%s, updated_at=now() "
                    "WHERE user_id=%s AND connection_id=%s",
                    (new_tok, uid, cid),
                )
        rotated += 1

    # ── ldap_config.bind_password (single row) ─────────────────────────────
    ldap_rotated = 0
    ldap_note = ""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bind_password FROM ldap_config WHERE id=1")
            lrow = cur.fetchone()
        if lrow and lrow[0]:
            ltok = lrow[0].encode() if isinstance(lrow[0], str) else lrow[0]
            try:
                mf.decrypt(ltok)
                if not args.verify:
                    new_l = mf.rotate(ltok).decode()
                    if not args.dry_run:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE ldap_config SET bind_password=%s, updated_at=now() "
                                "WHERE id=1", (new_l,))
                    ldap_rotated = 1
            except Exception:
                ldap_note = ("ldap_config.bind_password looks like legacy plaintext or "
                             "is unreadable; re-save the LDAP config once to encrypt it.")
    except Exception as e:
        ldap_note = "ldap_config not present/readable: %s" % str(e)[:80]

    if not args.dry_run and not args.verify:
        conn.commit()
    conn.close()

    print("decryptable: %d/%d" % (ok, len(rows)))
    if args.verify:
        print("VERIFY: all tokens open under the loaded keys"
              if not failed else "VERIFY FAILED: %d unreadable" % len(failed))
    else:
        print("re-encrypted: %d%s" % (rotated, " (dry-run, no writes)" if args.dry_run else ""))
        print("ldap bind_password: %s" % (
            "rotated" if ldap_rotated else (ldap_note or "none stored")))
    if failed:
        for f in failed[:10]:
            print("  FAIL user=%s conn=%s: %s" % f)
        sys.exit(1)


if __name__ == "__main__":
    main()
