"""Verifica di sola lettura del nuovo schema e dei conteggi per MAK P 206."""
import os
import psycopg2
from psycopg2.extras import RealDictCursor

def main():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("Imposta DATABASE_URL.")
    with psycopg2.connect(url, connect_timeout=30) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT e.id, e.name, e.event_date, e.status,
                       COUNT(DISTINCT g.id)::int AS guests,
                       COUNT(m.id) FILTER (WHERE m.movement_type='IN')::int AS entries,
                       COUNT(m.id) FILTER (WHERE m.movement_type='OUT')::int AS exits
                  FROM events e
                  LEFT JOIN event_guests g ON g.event_id=e.id
                  LEFT JOIN guest_movements m ON m.event_id=e.id AND m.guest_id=g.id
                 WHERE e.name='MAK P 206' AND e.event_date=DATE '2027-05-05'
                 GROUP BY e.id
            """)
            row = cur.fetchone()
            if not row:
                print("Evento MAK P 206 non trovato: eseguire prima la migrazione.")
                return
            for key, value in row.items():
                print(f"{key}: {value}")
            print(f"presenti (ingressi - uscite): {row['entries'] - row['exits']}")
if __name__ == "__main__":
    main()
