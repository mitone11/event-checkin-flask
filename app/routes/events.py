"""API multi-evento per Event Check-in."""
from functools import wraps
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import time

from flask import Blueprint, jsonify, request, session, current_app, send_file
from psycopg2 import errors
from openpyxl import load_workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

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


def _event_payload(row):
    return {"id": row["id"], "name": row["name"],
            "event_date": row["event_date"].isoformat() if row["event_date"] else None,
            "status": row["status"], "total_guests": row.get("total_guests", 0),
            "entered": row.get("entered", 0), "exited": row.get("exited", 0)}


def selected_event(required_status=None):
    event_id = session.get("event_id")
    if not event_id:
        return None, (jsonify(error="Seleziona prima un evento"), 409)
    with cursor() as cur:
        cur.execute("SELECT id,name,event_date,status FROM events WHERE id=%s", (event_id,))
        event = cur.fetchone()
    if not event:
        session.pop("event_id", None)
        return None, (jsonify(error="Evento selezionato non trovato"), 404)
    if required_status == "active" and event["status"] != "active":
        return None, (jsonify(error="Evento chiuso o archiviato: operazione non consentita"), 409)
    return event, None


def _selected(active=False):
    return selected_event("active" if active else None)


def _station(event, body=None):
    sid = session["session_id"]
    if session.get("role") == "station":
        with cursor() as cur:
            cur.execute("SELECT station FROM event_station_locks WHERE event_id=%s AND session_id=%s AND expires_at>%s", (event["id"], sid, int(time.time())))
            lock = cur.fetchone()
        if not lock:
            return None, (jsonify(error="Seleziona una postazione disponibile"), 409)
        return lock["station"], None
    value = (body or {}).get("station") or (body or {}).get("postazione")
    if value not in current_app.config["STATIONS"]:
        return None, (jsonify(error="Postazione non valida"), 400)
    return value, None


@bp.get("/events")
@authenticated
def list_events():
    with cursor() as cur:
        cur.execute("""
            SELECT e.id,e.name,e.event_date,e.status,
                   COUNT(DISTINCT g.id)::int AS total_guests,
                   COUNT(DISTINCT m.guest_id) FILTER (WHERE m.movement_type='IN')::int AS entered,
                   COUNT(DISTINCT m.guest_id) FILTER (WHERE m.movement_type='OUT')::int AS exited
              FROM events e LEFT JOIN event_guests g ON g.event_id=e.id
              LEFT JOIN guest_movements m ON m.event_id=e.id
             GROUP BY e.id ORDER BY e.event_date DESC,e.id DESC
        """)
        rows = cur.fetchall()
    return jsonify(events=[_event_payload(r) for r in rows], selected_event_id=session.get("event_id"))


@bp.post("/events/select")
@authenticated
def select_event():
    body = request.get_json(silent=True) or {}
    try:
        event_id = int(body.get("event_id"))
    except (TypeError, ValueError):
        return jsonify(error="ID evento non valido"), 400
    with cursor() as cur:
        cur.execute("SELECT id,name,event_date,status FROM events WHERE id=%s", (event_id,))
        event = cur.fetchone()
    if not event:
        return jsonify(error="Evento non trovato"), 404
    if session.get("role") == "station" and event["status"] != "active":
        return jsonify(error="Gli operatori possono selezionare soltanto eventi attivi"), 403
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM event_station_locks WHERE session_id=%s", (session["session_id"],))
    session["event_id"] = event_id
    return jsonify(success=True, event=_event_payload(event))


@bp.post("/admin/events")
@admin_required
def create_event():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    date_text = str(body.get("event_date") or "").strip()
    if not name or len(name) > 150 or not date_text:
        return jsonify(error="Nome e data sono obbligatori"), 400
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Data non valida"), 400
    try:
        with cursor(commit=True) as cur:
            cur.execute("INSERT INTO events(name,event_date,status) VALUES(%s,%s,'active') RETURNING id,name,event_date,status", (name, date_text))
            event = cur.fetchone()
    except errors.UniqueViolation:
        return jsonify(error="Esiste già un evento con questo nome e questa data"), 409
    return jsonify(success=True, event=_event_payload(event)), 201


@bp.patch("/admin/events/<int:event_id>/status")
@admin_required
def set_event_status(event_id):
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in {"active", "closed", "archived"}:
        return jsonify(error="Stato non valido"), 400
    with cursor(commit=True) as cur:
        cur.execute("UPDATE events SET status=%s,closed_at=CASE WHEN %s='active' THEN NULL ELSE COALESCE(closed_at,NOW()) END WHERE id=%s RETURNING id,name,event_date,status", (status,status,event_id))
        event = cur.fetchone()
    if not event:
        return jsonify(error="Evento non trovato"), 404
    if status != "active":
        with cursor(commit=True) as cur:
            cur.execute("DELETE FROM event_station_locks WHERE event_id=%s", (event_id,))
    return jsonify(success=True, event=_event_payload(event))


@bp.get("/event/guests")
@authenticated
def get_event_guests():
    event, err = _selected()
    if err: return err
    with cursor() as cur:
        cur.execute("""
            SELECT g.id,g.codice_identificativo,g.nome,g.cognome,g.invitante,g.numero_tavolo,g.note,
                   i.id AS in_id,i.occurred_at AS in_at,i.station AS in_station,
                   o.id AS out_id,o.occurred_at AS out_at,o.station AS out_station
              FROM event_guests g
              LEFT JOIN guest_movements i ON i.event_id=g.event_id AND i.guest_id=g.id AND i.movement_type='IN'
              LEFT JOIN guest_movements o ON o.event_id=g.event_id AND o.guest_id=g.id AND o.movement_type='OUT'
             WHERE g.event_id=%s ORDER BY g.cognome,g.nome,g.id
        """, (event["id"],))
        rows = cur.fetchall()
    guests=[]
    for r in rows:
        guests.append({"id":r["id"],"codice_identificativo":r["codice_identificativo"],"nome":r["nome"],"cognome":r["cognome"],"invitante":r["invitante"],"numero_tavolo":r["numero_tavolo"],"note":r["note"],
          "checkin":({"id":r["in_id"],"timestamp":r["in_at"].isoformat(),"postazione":r["in_station"]} if r["in_id"] else None),
          "checkout":({"id":r["out_id"],"timestamp":r["out_at"].isoformat(),"postazione":r["out_station"]} if r["out_id"] else None)})
    return jsonify(event=_event_payload(event),guests=guests)


def _guest_values(body):
    return {"codice_identificativo":str(body.get("codice_identificativo") or body.get("codice_carta_identita") or "").strip().upper(),
            "nome":str(body.get("nome") or "").strip(),"cognome":str(body.get("cognome") or "").strip(),
            "invitante":str(body.get("invitante") or "").strip(),"numero_tavolo":str(body.get("numero_tavolo") or "").strip(),"note":str(body.get("note") or "").strip()}


def _validate_guest(g):
    return bool(g["codice_identificativo"] and g["nome"] and g["cognome"] and len(g["codice_identificativo"])<=40 and len(g["nome"])<=100 and len(g["cognome"])<=100)


@bp.post("/admin/event/guests")
@admin_required
def add_guest():
    event,err=_selected(active=True)
    if err:return err
    g=_guest_values(request.get_json(silent=True) or {})
    if not _validate_guest(g): return jsonify(error="Codice identificativo, nome e cognome sono obbligatori (lunghezze non valide)"),400
    try:
        with cursor(commit=True) as cur:
            cur.execute("INSERT INTO event_guests(event_id,codice_identificativo,nome,cognome,invitante,numero_tavolo,note) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",(event["id"],*g.values()))
            g["id"]=cur.fetchone()["id"]
    except errors.UniqueViolation:return jsonify(error="Codice identificativo già presente nell'evento"),409
    return jsonify(success=True,guest=g),201


@bp.patch("/admin/event/guests/<int:guest_id>")
@admin_required
def edit_guest(guest_id):
    event,err=_selected(active=True)
    if err:return err
    g=_guest_values(request.get_json(silent=True) or {})
    if not _validate_guest(g):return jsonify(error="Codice, nome e cognome sono obbligatori"),400
    try:
        with cursor(commit=True) as cur:
            cur.execute("UPDATE event_guests SET codice_identificativo=%s,nome=%s,cognome=%s,invitante=%s,numero_tavolo=%s,note=%s WHERE event_id=%s AND id=%s RETURNING id",(*g.values(),event["id"],guest_id))
            row=cur.fetchone()
    except errors.UniqueViolation:return jsonify(error="Codice identificativo già utilizzato"),409
    if not row:return jsonify(error="Invitato non trovato"),404
    return jsonify(success=True,guest={"id":guest_id,**g})


@bp.delete("/admin/event/guests/<int:guest_id>")
@admin_required
def delete_guest(guest_id):
    event,err=_selected(active=True)
    if err:return err
    with cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM guest_movements WHERE event_id=%s AND guest_id=%s LIMIT 1",(event["id"],guest_id))
        if cur.fetchone():return jsonify(error="Impossibile eliminare: l'invitato ha movimenti registrati"),409
        cur.execute("DELETE FROM event_guests WHERE event_id=%s AND id=%s",(event["id"],guest_id))
        deleted=cur.rowcount
    if not deleted:return jsonify(error="Invitato non trovato"),404
    return jsonify(success=True)


@bp.get("/event/stats")
@authenticated
def stats():
    event,err=_selected()
    if err:return err
    with cursor() as cur:
        cur.execute("""
          SELECT COUNT(*)::int AS total,
                 COUNT(*) FILTER(WHERE i.id IS NOT NULL)::int AS entered,
                 COUNT(*) FILTER(WHERE o.id IS NOT NULL)::int AS exited,
                 COUNT(*) FILTER(WHERE i.id IS NULL)::int AS pending,
                 COUNT(*) FILTER(WHERE i.id IS NOT NULL AND o.id IS NULL)::int AS present
            FROM event_guests g
            LEFT JOIN guest_movements i ON i.event_id=g.event_id AND i.guest_id=g.id AND i.movement_type='IN'
            LEFT JOIN guest_movements o ON o.event_id=g.event_id AND o.guest_id=g.id AND o.movement_type='OUT'
           WHERE g.event_id=%s
        """,(event["id"],))
        counts=dict(cur.fetchone())
        cur.execute("SELECT station,COUNT(*)::int AS count FROM guest_movements WHERE event_id=%s AND movement_type='IN' GROUP BY station",(event["id"],))
        by_station={r["station"]:r["count"] for r in cur.fetchall()}
    return jsonify(**counts,byPostazione=by_station,event=_event_payload(event))


@bp.get("/event/stations")
@authenticated
def stations():
    event,err=_selected(active=(session.get("role")=="station"))
    if err:return err
    now=int(time.time())
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM event_station_locks WHERE expires_at<=%s",(now,))
        cur.execute("SELECT station,session_id FROM event_station_locks WHERE event_id=%s",(event["id"],))
        locks=cur.fetchall()
    sid=session["session_id"]
    own=next((r["station"] for r in locks if r["session_id"]==sid),None)
    return jsonify(current=own,stations=[{"name":n,"busy":any(r["station"]==n and r["session_id"]!=sid for r in locks),"selected":own==n} for n in current_app.config["STATIONS"]])


@bp.post("/event/stations/<path:station>/claim")
@authenticated
def claim_station(station):
    event,err=_selected(active=True)
    if err:return err
    if station not in current_app.config["STATIONS"]:return jsonify(error="Postazione non valida"),400
    now,sid=int(time.time()),session["session_id"]
    with connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)",(784223,))
                cur.execute("DELETE FROM event_station_locks WHERE expires_at<=%s",(now,))
                cur.execute("SELECT session_id FROM event_station_locks WHERE event_id=%s AND station=%s FOR UPDATE",(event["id"],station))
                occupied=cur.fetchone()
                if occupied and occupied[0]!=sid:
                    conn.rollback();return jsonify(error="Postazione già occupata"),409
                cur.execute("DELETE FROM event_station_locks WHERE session_id=%s",(sid,))
                cur.execute("INSERT INTO event_station_locks(event_id,station,session_id,expires_at) VALUES(%s,%s,%s,%s)",(event["id"],station,sid,now+current_app.config["STATION_LOCK_SECONDS"]))
            conn.commit()
        except Exception:
            conn.rollback();raise
    return jsonify(success=True,station=station,expiresIn=current_app.config["STATION_LOCK_SECONDS"])


@bp.post("/event/stations/heartbeat")
@authenticated
def station_heartbeat():
    event,err=_selected(active=True)
    if err:return err
    now=int(time.time())
    with cursor(commit=True) as cur:
        cur.execute("UPDATE event_station_locks SET expires_at=%s WHERE event_id=%s AND session_id=%s AND expires_at>%s",(now+current_app.config["STATION_LOCK_SECONDS"],event["id"],session["session_id"],now))
        changed=cur.rowcount
    if not changed:return jsonify(error="Prenotazione postazione scaduta"),409
    return jsonify(success=True,expiresIn=current_app.config["STATION_LOCK_SECONDS"])


@bp.delete("/event/stations/claim")
@authenticated
def release_station():
    with cursor(commit=True) as cur:cur.execute("DELETE FROM event_station_locks WHERE session_id=%s",(session["session_id"],))
    return jsonify(success=True)


@bp.post("/event/movements/<movement_type>")
@authenticated
def movement(movement_type):
    kind=movement_type.upper()
    if kind not in {"IN","OUT"}:return jsonify(error="Tipo movimento non valido"),400
    event,err=_selected(active=True)
    if err:return err
    body=request.get_json(silent=True) or {}
    try:gid=int(body.get("guest_id"))
    except (ValueError,TypeError):return jsonify(error="Invitato non valido"),400
    station,err=_station(event,body)
    if err:return err
    try:
        with cursor(commit=True) as cur:
            cur.execute("""INSERT INTO guest_movements(event_id,guest_id,movement_type,occurred_at,station,operator_session)
                         SELECT %s,g.id,%s,%s,%s,%s FROM event_guests g WHERE g.event_id=%s AND g.id=%s
                         RETURNING id,occurred_at,station""",(event["id"],kind,datetime.now(timezone.utc),station,session["session_id"],event["id"],gid))
            row=cur.fetchone()
    except errors.UniqueViolation:return jsonify(error="Movimento già registrato: è consentito un solo IN e un solo OUT"),409
    except errors.RaiseException as exc:return jsonify(error=str(exc).split("\n")[0]),409
    if not row:return jsonify(error="Invitato non trovato nell'evento selezionato"),404
    return jsonify(success=True,movement={"id":row["id"],"type":kind,"timestamp":row["occurred_at"].isoformat(),"station":row["station"]})


@bp.get("/event/movements")
@authenticated
def movement_history():
    event,err=_selected()
    if err:return err
    with cursor() as cur:
        cur.execute("""SELECT m.id,m.movement_type,m.occurred_at,m.station,g.nome,g.cognome,g.codice_identificativo
                       FROM guest_movements m JOIN event_guests g ON g.id=m.guest_id AND g.event_id=m.event_id
                       WHERE m.event_id=%s ORDER BY m.occurred_at DESC,m.id DESC LIMIT 2000""",(event["id"],))
        rows=cur.fetchall()
    return jsonify(movements=[{"id":r["id"],"type":r["movement_type"],"timestamp":r["occurred_at"].isoformat(),"station":r["station"],"nome":r["nome"],"cognome":r["cognome"],"codice_identificativo":r["codice_identificativo"]} for r in rows])


@bp.get("/event/guests/template")
@admin_required
def guest_template():
    path=Path(__file__).resolve().parents[2] / "modello_invitati_event_checkin.xlsx"
    return send_file(path,as_attachment=True,download_name="modello_invitati_event_checkin.xlsx")


@bp.post("/admin/event/guests/import")
@admin_required
def import_guests():
    event,err=_selected(active=True)
    if err:return err
    uploaded=request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith((".xlsx",".xlsm")):
        return jsonify(error="Carica un file Excel .xlsx"),400
    try:
        wb=load_workbook(uploaded,read_only=True,data_only=True)
        ws=wb.active
        values=list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception:return jsonify(error="File Excel non leggibile"),400
    if not values:return jsonify(error="Il file Excel è vuoto"),400
    headers=[str(x or "").strip().lower() for x in values[0]]
    aliases={"nome":"nome","cognome":"cognome","codice identificativo":"codice_identificativo","codice_identificativo":"codice_identificativo","invitato da":"invitante","invitante":"invitante","numero tavolo":"numero_tavolo","numero_tavolo":"numero_tavolo","note":"note"}
    idx={aliases[h]:i for i,h in enumerate(headers) if h in aliases}
    if not {"nome","cognome","codice_identificativo"}.issubset(idx):return jsonify(error="Intestazioni mancanti: Nome, Cognome, Codice identificativo"),400
    parsed=[];seen=set();invalid=[]
    for line,row in enumerate(values[1:],start=2):
        if not any(v is not None and str(v).strip() for v in row):continue
        def val(key):
            i=idx.get(key);return str(row[i] if i is not None and i<len(row) and row[i] is not None else "").strip()
        g={"nome":val("nome"),"cognome":val("cognome"),"codice_identificativo":val("codice_identificativo").upper(),"invitante":val("invitante"),"numero_tavolo":val("numero_tavolo"),"note":val("note")}
        if not _validate_guest(g):invalid.append({"row":line,"reason":"Nome, cognome o codice mancante/non valido"});continue
        if g["codice_identificativo"] in seen:invalid.append({"row":line,"reason":"Codice duplicato nel file"});continue
        seen.add(g["codice_identificativo"]);parsed.append(g)
    with cursor() as cur:
        cur.execute("SELECT codice_identificativo FROM event_guests WHERE event_id=%s",(event["id"],))
        existing={r["codice_identificativo"] for r in cur.fetchall()}
    duplicates=[g["codice_identificativo"] for g in parsed if g["codice_identificativo"] in existing]
    ready=[g for g in parsed if g["codice_identificativo"] not in existing]
    confirm=request.form.get("confirm")=="true"
    if not confirm:
        return jsonify(preview=True,total_rows=len(parsed),to_import=len(ready),duplicates=duplicates,invalid=invalid,columns=list(idx.keys())),200
    if not ready:return jsonify(success=True,imported=0,duplicates=duplicates,invalid=invalid)
    with cursor(commit=True) as cur:
        for g in ready:
            cur.execute("INSERT INTO event_guests(event_id,codice_identificativo,nome,cognome,invitante,numero_tavolo,note) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,codice_identificativo) DO NOTHING",(event["id"],g["codice_identificativo"],g["nome"],g["cognome"],g["invitante"],g["numero_tavolo"],g["note"]))
    return jsonify(success=True,imported=len(ready),duplicates=duplicates,invalid=invalid)


@bp.get("/event/report.pdf")
@admin_required
def event_report():
    event,err=_selected()
    if err:return err
    with cursor() as cur:
        cur.execute("""SELECT g.nome,g.cognome,g.codice_identificativo,g.invitante,g.numero_tavolo,
                       i.occurred_at AS entered_at,i.station AS in_station,o.occurred_at AS exited_at,o.station AS out_station
                       FROM event_guests g LEFT JOIN guest_movements i ON i.event_id=g.event_id AND i.guest_id=g.id AND i.movement_type='IN'
                       LEFT JOIN guest_movements o ON o.event_id=g.event_id AND o.guest_id=g.id AND o.movement_type='OUT'
                       WHERE g.event_id=%s ORDER BY g.cognome,g.nome""",(event["id"],))
        rows=cur.fetchall()
    total=len(rows);entered=sum(1 for r in rows if r["entered_at"]);exited=sum(1 for r in rows if r["exited_at"])
    buff=BytesIO();doc=SimpleDocTemplate(buff,pagesize=landscape(A4),rightMargin=24,leftMargin=24,topMargin=24,bottomMargin=24)
    styles=getSampleStyleSheet();story=[Paragraph("Report evento: "+event["name"],styles["Title"]),Paragraph("Data: "+event["event_date"].strftime("%d/%m/%Y")+" | Stato: "+event["status"],styles["Normal"]),Spacer(1,10),Paragraph(f"Invitati: {total} | Ingressi: {entered} | Uscite: {exited} | Presenti: {entered-exited}",styles["Heading2"]),Spacer(1,10)]
    data=[["Cognome","Nome","Codice","Invitante","Tavolo","Ingresso","Postazione IN","Uscita","Postazione OUT"]]
    for r in rows:
        fmt=lambda d:d.strftime("%d/%m/%Y %H:%M:%S") if d else "—"
        data.append([r["cognome"],r["nome"],r["codice_identificativo"],r["invitante"],r["numero_tavolo"],fmt(r["entered_at"]),r["in_station"] or "—",fmt(r["exited_at"]),r["out_station"] or "—"])
    table=Table(data,repeatRows=1,colWidths=[75,75,75,95,45,95,70,95,70])
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#243247")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),.25,colors.grey),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#eef2f6")]),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    story.append(table);doc.build(story);buff.seek(0)
    return send_file(buff,mimetype="application/pdf",as_attachment=True,download_name=f"report_{event['id']}.pdf")
