# Event Check-in - Fase 1 multi-evento

Questa consegna aggiunge lo schema PostgreSQL multi-evento e migra i dati legacy
nell'evento `MAK P 206` del 5 maggio 2027.

## Importante

- La migrazione è additiva: NON elimina e NON modifica `guests` e `checkins`.
- La migrazione deve essere eseguita una volta sul database Neon corretto.
- Fare prima un backup Neon.
- Non distribuire ancora questa fase come nuova app operativa multi-evento:
  le API e l'interfaccia esistenti sono ancora quelle del vecchio modello singolo-evento.
  Questa è la prima tranche (database e migrazione); le API, le schermate,
  l'importazione Excel e i PDF saranno la fase successiva.
- Se la migrazione rileva un timestamp storico non valido o un check-in senza
  invitato corrispondente, esegue rollback e lascia il database invariato.

## Tabelle create

- `events`
- `event_guests`
- `guest_movements`
- `event_station_locks`
- `schema_migrations`

Gli invitati sono isolati tramite `event_id`. Un invitato può avere un solo IN e
un solo OUT per evento. Un OUT senza IN è rifiutato dal trigger PostgreSQL.

## Esecuzione da Windows CMD

Dalla cartella del progetto:

```cmd
py -m venv .venv
.venv\Scripts\activate
pip install psycopg2-binary
set DATABASE_URL=INCOLLA_QUI_LA_CONNECTION_STRING_NEON
python migrate_multi_event.py
python verify_multi_event.py
```

Usare la connection string Neon con `sslmode=require`. Non inserirla in file
committati e non inviarla in chat.

## Esecuzione su Neon SQL Editor

In alternativa, aprire `migrations/001_multi_event_schema.sql` e incollare il
contenuto nel SQL Editor di Neon. Questo crea soltanto lo schema, NON migra gli
invitati legacy: per migrare anche i dati serve eseguire `migrate_multi_event.py`.

Non eseguire entrambe le modalità in parallelo. Il metodo raccomandato è lo
script Python, che applica schema e trasferimento dati nella stessa transazione.

## Git

Dopo aver copiato questi file nella radice del repository Flask:

```cmd
git status
git add migrations migrate_multi_event.py verify_multi_event.py
git commit -m "Add multi-event database schema and legacy migration"
git push
```

Il deploy Render può avvenire dopo il push, ma non sostituisce l'esecuzione
manuale della migrazione sul database Neon. Non aggiungere la migrazione
automaticamente all'avvio di Gunicorn.

## Modello Excel

È incluso `modello_invitati_event_checkin.xlsx` e lo script
`tools/create_guest_template.py` per rigenerarlo. La generazione automatica
dal pannello admin sarà collegata nella fase applicativa successiva.

Per rigenerarlo:
```cmd
pip install -r requirements-additions.txt
python tools\create_guest_template.py
```
