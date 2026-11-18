"""Esegue la migrazione iniziale multi-evento e importa il legacy nel MAK P 206.

Uso:
    set DATABASE_URL=postgresql://...
    python migrate_multi_event.py

La migrazione è transazionale e idempotente. Non elimina né modifica guests/checkins.
"""
import os
from pathlib import Path
import sys
import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parent
MIGRATION_VERSION = "001_multi_event_schema"
EVENT_NAME = "MAK P 206"
EVENT_DATE = "2027-05-05"

def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("ERRORE: imposta DATABASE_URL nell'ambiente prima di procedere.")

    sql = (ROOT / "migrations" / "001_multi_event_schema.sql").read_text(encoding="utf-8")
    conn = psycopg2.connect(database_url, connect_timeout=30)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (908172635,))
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(100) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (MIGRATION_VERSION,))
            if cur.fetchone():
                conn.commit()
                print("Migrazione già applicata: nessuna modifica.")
                return

            cur.execute(sql)

            # Verifica che il database contenga le tabelle legacy attese.
            cur.execute("""
                SELECT to_regclass('public.guests') AS guests_table,
                       to_regclass('public.checkins') AS checkins_table
            """)
            legacy = cur.fetchone()
            if not legacy["guests_table"]:
                raise RuntimeError("Tabella legacy public.guests non trovata: migrazione interrotta.")
            if not legacy["checkins_table"]:
                raise RuntimeError("Tabella legacy public.checkins non trovata: migrazione interrotta.")

            cur.execute("""
                INSERT INTO events (name, event_date, status)
                VALUES (%s, %s::date, 'active')
                ON CONFLICT (name, event_date) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
            """, (EVENT_NAME, EVENT_DATE))
            event_id = cur.fetchone()["id"]

            cur.execute("""
                SELECT codice_carta_identita, nome, cognome, invitante, numero_tavolo
                  FROM guests
                 ORDER BY cognome, nome
            """)
            legacy_guests = cur.fetchall()

            migrated_guests = 0
            for guest in legacy_guests:
                code = (guest["codice_carta_identita"] or "").strip()
                if not code:
                    continue
                cur.execute("""
                    INSERT INTO event_guests
                        (event_id, legacy_guest_code, codice_identificativo,
                         nome, cognome, invitante, numero_tavolo)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id, codice_identificativo) DO NOTHING
                    RETURNING id
                """, (
                    event_id, code, code,
                    (guest["nome"] or "").strip(),
                    (guest["cognome"] or "").strip(),
                    (guest["invitante"] or "").strip(),
                    (guest["numero_tavolo"] or "").strip()
                ))
                if cur.fetchone():
                    migrated_guests += 1

            # Importa gli ingressi storici. Il timestamp legacy è testo ISO:
            # se un valore non è convertibile PostgreSQL interrompe e rollbacka tutto.
            cur.execute("""
                SELECT c.codice_carta_identita, c.timestamp, c.postazione
                  FROM checkins c
                 ORDER BY c.timestamp
            """)
            legacy_checkins = cur.fetchall()
            migrated_entries = 0
            for checkin in legacy_checkins:
                code = (checkin["codice_carta_identita"] or "").strip()
                if not code:
                    continue
                cur.execute("""
                    SELECT id FROM event_guests
                     WHERE event_id = %s AND codice_identificativo = %s
                """, (event_id, code))
                guest_row = cur.fetchone()
                if not guest_row:
                    # Dati legacy incoerenti: non inventiamo un invitato mancante.
                    raise RuntimeError(
                        f"Check-in legacy senza invitato corrispondente: {code!r}"
                    )
                timestamp = checkin["timestamp"]
                if not timestamp:
                    raise RuntimeError(
                        f"Timestamp mancante per il check-in legacy {code!r}; "
                        "correggere il dato prima della migrazione."
                    )
                cur.execute("""
                    INSERT INTO guest_movements
                        (event_id, guest_id, movement_type, occurred_at, station)
                    VALUES (%s, %s, 'IN', %s::timestamptz, %s)
                    ON CONFLICT (event_id, guest_id, movement_type) DO NOTHING
                """, (
                    event_id, guest_row["id"], timestamp,
                    (checkin["postazione"] or "Postazione non specificata").strip()
                ))
                migrated_entries += cur.rowcount

            cur.execute("""
                INSERT INTO schema_migrations (version)
                VALUES (%s) ON CONFLICT (version) DO NOTHING
            """, (MIGRATION_VERSION,))

            cur.execute("SELECT COUNT(*)::int AS n FROM event_guests WHERE event_id = %s", (event_id,))
            total_guests = cur.fetchone()["n"]
            cur.execute("""
                SELECT COUNT(*)::int AS n FROM guest_movements
                 WHERE event_id = %s AND movement_type = 'IN'
            """, (event_id,))
            total_entries = cur.fetchone()["n"]

        conn.commit()
        print("Migrazione completata.")
        print(f"Evento: {EVENT_NAME} ({EVENT_DATE}) - ID {event_id}")
        print(f"Invitati presenti nell'evento: {total_guests}")
        print(f"Ingressi storici importati in questa esecuzione: {migrated_entries}")
        print(f"Ingressi totali associati all'evento: {total_entries}")
        print("Le tabelle legacy guests e checkins sono state lasciate intatte.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
