-- Event Check-in - Fase 1: schema multi-evento
-- Migrazione additiva: non elimina né modifica le tabelle legacy guests/checkins.

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    event_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'closed', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    UNIQUE (name, event_date)
);

CREATE TABLE IF NOT EXISTS event_guests (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    legacy_guest_code TEXT,
    codice_identificativo VARCHAR(40) NOT NULL,
    nome VARCHAR(100) NOT NULL,
    cognome VARCHAR(100) NOT NULL,
    invitante VARCHAR(150) NOT NULL DEFAULT '',
    numero_tavolo VARCHAR(100) NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_event_guest_code UNIQUE (event_id, codice_identificativo),
    CONSTRAINT uq_event_guest_pair UNIQUE (event_id, id)
);

CREATE INDEX IF NOT EXISTS idx_event_guests_event_name
    ON event_guests (event_id, cognome, nome);
CREATE INDEX IF NOT EXISTS idx_event_guests_event_inviter
    ON event_guests (event_id, invitante);

CREATE TABLE IF NOT EXISTS guest_movements (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL,
    guest_id BIGINT NOT NULL,
    movement_type VARCHAR(3) NOT NULL CHECK (movement_type IN ('IN', 'OUT')),
    occurred_at TIMESTAMPTZ NOT NULL,
    station VARCHAR(50) NOT NULL,
    operator_session VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_movement_guest_event
        FOREIGN KEY (event_id, guest_id)
        REFERENCES event_guests (event_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_guest_movement_type UNIQUE (event_id, guest_id, movement_type)
);

CREATE INDEX IF NOT EXISTS idx_movements_event_time
    ON guest_movements (event_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_movements_guest
    ON guest_movements (event_id, guest_id, occurred_at);

CREATE TABLE IF NOT EXISTS event_station_locks (
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    station VARCHAR(50) NOT NULL,
    session_id VARCHAR(100) NOT NULL UNIQUE,
    expires_at BIGINT NOT NULL,
    PRIMARY KEY (event_id, station)
);
CREATE INDEX IF NOT EXISTS idx_event_station_locks_expiry
    ON event_station_locks (expires_at);

-- Impedisce OUT senza IN e protegge la sequenza anche con richieste concorrenti.
CREATE OR REPLACE FUNCTION enforce_guest_movement_order()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM 1
      FROM event_guests
     WHERE id = NEW.guest_id AND event_id = NEW.event_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Invitato non valido per questo evento';
    END IF;

    IF NEW.movement_type = 'OUT' AND NOT EXISTS (
        SELECT 1 FROM guest_movements
         WHERE event_id = NEW.event_id
           AND guest_id = NEW.guest_id
           AND movement_type = 'IN'
    ) THEN
        RAISE EXCEPTION 'Non è possibile registrare un''uscita senza ingresso';
    END IF;

    IF NEW.movement_type = 'IN' AND EXISTS (
        SELECT 1 FROM guest_movements
         WHERE event_id = NEW.event_id
           AND guest_id = NEW.guest_id
           AND movement_type = 'OUT'
    ) THEN
        RAISE EXCEPTION 'L''invitato risulta già uscito';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_guest_movement_order ON guest_movements;
CREATE TRIGGER trg_enforce_guest_movement_order
BEFORE INSERT ON guest_movements
FOR EACH ROW EXECUTE FUNCTION enforce_guest_movement_order();

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(100) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
