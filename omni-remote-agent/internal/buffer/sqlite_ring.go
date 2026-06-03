package buffer

import (
	"database/sql"
	"fmt"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

const schema = `
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    payload  BLOB    NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
`

// Event is a single buffered evidence record.
type Event struct {
	ID       int64
	Ts       int64
	Payload  []byte
	Attempts int
}

// RingBuffer is a bounded SQLite-backed event queue with WAL mode.
type RingBuffer struct {
	db        *sql.DB
	maxEvents int
}

// Open creates (or opens) the SQLite database at path, initializes the schema,
// enables WAL journal mode, and returns a ready RingBuffer.
func Open(path string, maxEvents int) (*RingBuffer, error) {
	db, err := sql.Open("sqlite3", path+"?_busy_timeout=5000")
	if err != nil {
		return nil, fmt.Errorf("buffer: open db: %w", err)
	}
	// Single writer — avoid SQLITE_BUSY under concurrent access.
	db.SetMaxOpenConns(1)

	if _, err := db.Exec("PRAGMA journal_mode=WAL;"); err != nil {
		db.Close()
		return nil, fmt.Errorf("buffer: set WAL: %w", err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("buffer: create schema: %w", err)
	}
	return &RingBuffer{db: db, maxEvents: maxEvents}, nil
}

// Insert stores a new payload and evicts the oldest rows when the ring is full.
func (r *RingBuffer) Insert(payload []byte) error {
	ts := time.Now().UnixMilli()
	_, err := r.db.Exec(
		`INSERT INTO events (ts, payload) VALUES (?, ?)`,
		ts, payload,
	)
	if err != nil {
		return fmt.Errorf("buffer: insert: %w", err)
	}
	// Trim oldest rows to stay within maxEvents.
	_, err = r.db.Exec(
		`DELETE FROM events WHERE id IN (
            SELECT id FROM events ORDER BY id ASC
            LIMIT MAX(0, (SELECT COUNT(*) FROM events) - ?)
        )`,
		r.maxEvents,
	)
	if err != nil {
		return fmt.Errorf("buffer: trim: %w", err)
	}
	return nil
}

// Scan returns up to limit events ordered from oldest to newest.
func (r *RingBuffer) Scan(limit int) ([]Event, error) {
	rows, err := r.db.Query(
		`SELECT id, ts, payload, attempts FROM events ORDER BY id ASC LIMIT ?`,
		limit,
	)
	if err != nil {
		return nil, fmt.Errorf("buffer: scan: %w", err)
	}
	defer rows.Close()

	var events []Event
	for rows.Next() {
		var e Event
		if err := rows.Scan(&e.ID, &e.Ts, &e.Payload, &e.Attempts); err != nil {
			return nil, fmt.Errorf("buffer: scan row: %w", err)
		}
		events = append(events, e)
	}
	return events, rows.Err()
}

// Delete removes events by their IDs.
func (r *RingBuffer) Delete(ids []int64) error {
	if len(ids) == 0 {
		return nil
	}
	placeholders := strings.Repeat("?,", len(ids))
	placeholders = placeholders[:len(placeholders)-1] // strip trailing comma
	query := fmt.Sprintf(`DELETE FROM events WHERE id IN (%s)`, placeholders)
	args := int64SliceToAny(ids)
	if _, err := r.db.Exec(query, args...); err != nil {
		return fmt.Errorf("buffer: delete: %w", err)
	}
	return nil
}

// IncrAttempts increments the attempt counter for the given event IDs.
func (r *RingBuffer) IncrAttempts(ids []int64) error {
	if len(ids) == 0 {
		return nil
	}
	placeholders := strings.Repeat("?,", len(ids))
	placeholders = placeholders[:len(placeholders)-1]
	query := fmt.Sprintf(
		`UPDATE events SET attempts = attempts + 1 WHERE id IN (%s)`,
		placeholders,
	)
	args := int64SliceToAny(ids)
	if _, err := r.db.Exec(query, args...); err != nil {
		return fmt.Errorf("buffer: incr_attempts: %w", err)
	}
	return nil
}

// Close closes the underlying database connection.
func (r *RingBuffer) Close() error {
	return r.db.Close()
}

// int64SliceToAny converts []int64 to []any for use as variadic sql args.
func int64SliceToAny(ids []int64) []any {
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	return args
}
