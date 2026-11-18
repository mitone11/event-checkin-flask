"""API multi-evento. Le route legacy restano disponibili durante la transizione UI."""
from functools import wraps
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session, current_app
from psycopg2 import errors

from app.db import connection, cursor

bp = Blueprint("events", __name__, url_prefix="/api")


def authenticated(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("role") or not session.get("session_id"):
            return jsonify(error="Accesso richiesto"), 401
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @authenticated
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify(error="Funzione riservata all’amministratore"), 403
        return fn(*args, **kwargs)
    return wrapper


def selected_event(required_status=None):
    event_id = session.get("event_id")
    if not event_id:
        return None, (jsonify(error="Seleziona prima un evento"), 409)
    with cursor() as cur:
        cur.execute("SELECT id, name, event_date, status FROM events WHERE id = %s", (event_id,))
        event = cur.fetchone()
    if not event:
        session.pop("event_id", None)
        return None, (jsonify(error="Evento selezionato non trovato"), 404)
    if required_status == "active" and event["status"] != "active":
        return None, (jsonify(error="L’evento è chiuso o archiviato: operazione non consentita"), 409)
    return event, None


def _event_payload(row):
    return {
        "id": row["id"], "name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
        "status": row["status"],
        "total_guests": row.get("total_guests", 0),
        "entered": row.get("entered", 0),
        "exited": row.get("exited", 0),
    }


@bp.get("/events")
@authenticated
def list_events():
    with cursor() as cur:
        cur.execute("""
            SELECT e.id, e.name, e.event_date, e.status,
                   COUNT(DISTINCT g.id)::int AS total_guests,
                   COUNT(DISTINCT m.guest_id) FILTER (WHERE m.movement_type='IN')::int AS entered,
                   COUNT(DISTINCT m.guest_id) FILTER (WHERE m.movement_type='OUT')::int AS exited
              FROM events e
              LEFT JOIN event_guests g ON g.event_id=e.id
              LEFT JOIN guest_movements m ON m.event_id=e.id
             GROUP BY e.id
             ORDER BY e.event_date DESC, e.id DESC
        """)
        rows = cur.fetchall()
    return jsonify(events=[_event_payload(r) for r in rows], selected_event_id=session.get("event_id"))


@bp.post("/events/select")
@authenticated
def select_event():
    body = request.get_json(silent=True) or {}
    event_id = body.get("event_id")
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        return jsonify(error="ID evento non valido"), 400
    with cursor() as cur:
        cur.execute("SELECT id, name, event_date, status FROM events WHERE id=%s", (event_id,))
        event = cur.fetchone()
    if not event:
        return jsonify(error="Evento non trovato"), 404
    if session.get("role") == "station" and event["status"] != "active":
        return jsonify(error="Gli operatori possono selezionare soltanto eventi attivi"), 403
    # Un cambio evento rilascia la postazione eventualmente occupata nel precedente.
    sid = session.get("session_id")
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM event_station_locks WHERE session_id=%s", (sid,))
    session["event_id"] = event_id
    return jsonify(success=True, event=_event_payload(event))


@bp.post("/admin/events")
@admin_required
def create_event():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    event_date = str(body.get("event_date") or "").strip()
    if not name or len(name) > 150 or not event_date:
        return jsonify(error="Nome e data dell’evento sono obbligatori"), 400
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Data non valida: usa YYYY-MM-DD"), 400
    try:
        with cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO events (name, event_date, status)
                VALUES (%s, %s, 'active')
                RETURNING id, name, event_date, status
            """, (name, event_date))
            event = cur.fetchone()
    except errors.UniqueViolation:
        return jsonify(error="Esiste già un evento con lo stesso nome e la stessa data"), 409
    return jsonify(success=True, event=_event_payload(event)), 201


@bp.patch("/admin/events/<int:event_id>/status")
@admin_required
def update_event_status(event_id):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in {"active", "closed", "archived"}:
        return jsonify(error="Stato non valido"), 400
    with cursor(commit=True) as cur:
        cur.execute("""
            UPDATE events
               SET status=%s,
                   closed_at=CASE WHEN %s='active' THEN NULL ELSE COALESCE(closed_at, NOW()) END
             WHERE id=%s
         RETURNING id, name, event_date, status
        """, (status, status, event_id))
        event = cur.fetchone()
    if not event:
        return jsonify(error="Evento non trovato"), 404
    if status != "active":
        with cursor(commit=True) as cur:
            cur.execute("DELETE FROM event_station_locks WHERE event_id=%s", (event_id,))
    return jsonify(success=True, event=_event_payload(event))


def _require_selected(active=False):
    event, error = selected_event("active" if active else None)
    if error:
        return None, error
    return event, None


@bp.get("/event/guests")
@authenticated
def event_guests():
    event, error = _require_selected()
    if error:
        return error
    with cursor() as cur:
        cur.execute("""
            SELECT g.id, g.codice_identificativo, g.nome, g.cognome,
                   g.invitante, g.numero_tavolo, g.note,
                   i.occurred_at AS checkin_at, i.station AS checkin_station,
                   o.occurred_at AS checkout_at, o.station AS checkout_station
              FROM event_guests g
              LEFT JOIN guest_movements i ON i.event_id=g.event_id AND i.guest_id=g.id AND i.movement_type='IN'
              LEFT JOIN guest_movements o ON o.event_id=g.event_id AND o.guest_id=g.id AND o.movement_type='OUT'
             WHERE g.event_id=%s
             ORDER BY g.cognome, g.nome, g.id
        """, (event["id"],))
        rows = cur.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"], "codice_identificativo": r["codice_identificativo"],
            "nome": r["nome"], "cognome": r["cognome"],
            "invitante": r["invitante"], "numero_tavolo": r["numero_tavolo"], "note": r["note"],
            "checkin": ({"timestamp": r["checkin_at"].isoformat(), "postazione": r["checkin_station"]} if r["checkin_at"] else None),
            "checkout": ({"timestamp": r["checkout_at"].isoformat(), "postazione": r["checkout_station"]} if r["checkout_at"] else None),
        })
    return jsonify(event=_event_payload(event), guests=result)


@bp.post("/admin/event/guests")
@admin_required
def add_event_guest():
    event, error = _require_selected(active=True)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    guest = {
        "codice_identificativo": str(body.get("codice_identificativo") or body.get("codice_carta_identita") or "").strip().upper(),
        "nome": str(body.get("nome") or "").strip(),
        "cognome": str(body.get("cognome") or "").strip(),
        "invitante": str(body.get("invitante") or "").strip(),
        "numero_tavolo": str(body.get("numero_tavolo") or "").strip(),
        "note": str(body.get("note") or "").strip(),
    }
    if not guest["codice_identificativo"] or not guest["nome"] or not guest["cognome"]:
        return jsonify(error="Codice identificativo, nome e cognome sono obbligatori"), 400
    try:
        with cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO event_guests (event_id, codice_identificativo, nome, cognome, invitante, numero_tavolo, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (event["id"], guest["codice_identificativo"], guest["nome"], guest["cognome"], guest["invitante"], guest["numero_tavolo"], guest["note"]))
            guest["id"] = cur.fetchone()["id"]
    except errors.UniqueViolation:
        return jsonify(error="Esiste già un invitato con questo codice in questo evento"), 409
    return jsonify(success=True, guest=guest), 201


@bp.patch("/admin/event/guests/<int:guest_id>")
@admin_required
def update_event_guest(guest_id):
    event, error = _require_selected(active=True)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    fields = {
        "codice_identificativo": str(body.get("codice_identificativo") or "").strip().upper(),
        "nome": str(body.get("nome") or "").strip(),
        "cognome": str(body.get("cognome") or "").strip(),
        "invitante": str(body.get("invitante") or "").strip(),
        "numero_tavolo": str(body.get("numero_tavolo") or "").strip(),
        "note": str(body.get("note") or "").strip(),
    }
    if not fields["codice_identificativo"] or not fields["nome"] or not fields["cognome"]:
        return jsonify(error="Codice identificativo, nome e cognome sono obbligatori"), 400
    try:
        with cursor(commit=True) as cur:
            cur.execute("""
                UPDATE event_guests SET codice_identificativo=%s, nome=%s, cognome=%s,
                       invitante=%s, numero_tavolo=%s, note=%s
                 WHERE event_id=%s AND id=%s RETURNING id
            """, (*fields.values(), event["id"], guest_id))
            updated = cur.fetchone()
    except errors.UniqueViolation:
        return jsonify(error="Un altro invitato usa già questo codice nell’evento"), 409
    if not updated:
        return jsonify(error="Invitato non trovato nell’evento selezionato"), 404
    return jsonify(success=True, guest={"id": guest_id, **fields})


@bp.delete("/admin/event/guests/<int:guest_id>")
@admin_required
def delete_event_guest(guest_id):
    event, error = _require_selected(active=True)
    if error:
        return error
    with cursor(commit=True) as cur:
        cur.execute("""SELECT 1 FROM guest_movements
                       WHERE event_id=%s AND guest_id=%s LIMIT 1""", (event["id"], guest_id))
        has_movements = cur.fetchone()
        if has_movements:
            return jsonify(error="Non puoi eliminare un invitato con movimenti registrati. Correzione riservata all’amministratore."), 409
        cur.execute("DELETE FROM event_guests WHERE event_id=%s AND id=%s", (event["id"], guest_id))
        deleted = cur.rowcount
    if not deleted:
        return jsonify(error="Invitato non trovato nell’evento selezionato"), 404
    return jsonify(success=True)


@bp.get("/event/stats")
@authenticated
def event_stats():
    event, error = _require_selected()
    if error:
        return error
    with cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)::int AS total,
                   COUNT(*) FILTER (WHERE i.id IS NOT NULL)::int AS entered,
                   COUNT(*) FILTER (WHERE o.id IS NOT NULL)::int AS exited,
                   COUNT(*) FILTER (WHERE i.id IS NULL)::int AS pending
              FROM event_guests g
              LEFT JOIN guest_movements i ON i.event_id=g.event_id AND i.guest_id=g.id AND i.movement_type='IN'
              LEFT JOIN guest_movements o ON o.event_id=g.event_id AND o.guest_id=g.id AND o.movement_type='OUT'
             WHERE g.event_id=%s
        """, (event["id"],))
        counts = cur.fetchone()
        cur.execute("""
            SELECT station, COUNT(*)::int AS count
              FROM guest_movements
             WHERE event_id=%s AND movement_type='IN'
             GROUP BY station
        """, (event["id"],))
        stations = {r["station"]: r["count"] for r in cur.fetchall()}
    return jsonify(**dict(counts), byPostazione=stations, event=_event_payload(event))


def _station_for_request(event, body=None):
    sid = session["session_id"]
    if session.get("role") == "station":
        with cursor() as cur:
            cur.execute("""SELECT station FROM event_station_locks
                           WHERE event_id=%s AND session_id=%s AND expires_at>%s""", (event["id"], sid, int(time.time())))
            lock = cur.fetchone()
        if not lock:
            return None, (jsonify(error="Seleziona una postazione disponibile prima di registrare il movimento"), 409)
        return lock["station"], None
    station = (body or {}).get("station") or (body or {}).get("postazione")
    if station not in current_app.config["STATIONS"]:
        return None, (jsonify(error="Postazione non valida"), 400)
    return station, None


@bp.get("/event/stations")
@authenticated
def event_stations():
    event, error = _require_selected(active=(session.get("role") == "station"))
    if error:
        return error
    now = int(time.time())
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM event_station_locks WHERE expires_at<=%s", (now,))
        cur.execute("SELECT station, session_id FROM event_station_locks WHERE event_id=%s", (event["id"]))
        locks = cur.fetchall()
    owned = next((r["station"] for r in locks if r["session_id"] == session["session_id"]), None)
    return jsonify(current=owned, stations=[{
        "name": name,
        "busy": any(r["station"] == name and r["session_id"] != session["session_id"] for r in locks),
        "selected": owned == name,
    } for name in current_app.config["STATIONS"]])


@bp.post("/event/stations/<path:station>/claim")
@authenticated
def claim_event_station(station):
    event, error = _require_selected(active=True)
    if error:
        return error
    if station not in current_app.config["STATIONS"]:
        return jsonify(error="Postazione non valida"), 400
    now, sid = int(time.time()), session["session_id"]
    with connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (784223,))
                cur.execute("DELETE FROM event_station_locks WHERE expires_at<=%s", (now,))
                cur.execute("SELECT session_id FROM event_station_locks WHERE event_id=%s AND station=%s FOR UPDATE", (event["id"], station))
                occupied = cur.fetchone()
                if occupied and occupied[0] != sid:
                    conn.rollback()
                    return jsonify(error="Questa postazione è già in uso. Selezionane un’altra."), 409
                cur.execute("DELETE FROM event_station_locks WHERE session_id=%s", (sid,))
                cur.execute("INSERT INTO event_station_locks (event_id, station, session_id, expires_at) VALUES (%s,%s,%s,%s)", (event["id"], station, sid, now + current_app.config["STATION_LOCK_SECONDS"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return jsonify(success=True, station=station, expiresIn=current_app.config["STATION_LOCK_SECONDS"])


@bp.post("/event/stations/heartbeat")
@authenticated
def heartbeat_event_station():
    event, error = _require_selected(active=True)
    if error:
        return error
    with cursor(commit=True) as cur:
        cur.execute("UPDATE event_station_locks SET expires_at=%s WHERE event_id=%s AND session_id=%s AND expires_at>%s", (int(time.time()) + current_app.config["STATION_LOCK_SECONDS"], event["id"], session["session_id"], int(time.time())))
        updated = cur.rowcount
    if not updated:
        return jsonify(error="La postazione non è più riservata a questa sessione"), 409
    return jsonify(success=True, expiresIn=current_app.config["STATION_LOCK_SECONDS"])


@bp.delete("/event/stations/claim")
@authenticated
def release_event_station():
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM event_station_locks WHERE session_id=%s", (session["session_id"],))
    return jsonify(success=True)


@bp.post("/event/movements/<movement_type>")
@authenticated
def create_movement(movement_type):
    movement_type = movement_type.upper()
    if movement_type not in {"IN", "OUT"}:
        return jsonify(error="Tipo movimento non valido"), 400
    event, error = _require_selected(active=True)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        guest_id = int(body.get("guest_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invitato non valido"), 400
    station, error = _station_for_request(event, body)
    if error:
        return error
    stamp = datetime.now(timezone.utc)
    try:
        with cursor(commit=True) as cur:
            cur.execute("""INSERT INTO guest_movements
                (event_id, guest_id, movement_type, occurred_at, station, operator_session)
                SELECT %s, g.id, %s, %s, %s, %s
                  FROM event_guests g WHERE g.event_id=%s AND g.id=%s
                RETURNING id, occurred_at, station""",
                (event["id"], movement_type, stamp, station, session["session_id"], event["id"], guest_id))
            movement = cur.fetchone()
    except errors.UniqueViolation:
        return jsonify(error="Questo movimento è già stato registrato"), 409
    except errors.RaiseException as exc:
        return jsonify(error=str(exc).split("\n")[0]), 409
    if not movement:
        return jsonify(error="Invitato non trovato nell’evento selezionato"), 404
    return jsonify(success=True, movement={"id": movement["id"], "type": movement_type, "timestamp": movement["occurred_at"].isoformat(), "station": movement["station"]})
