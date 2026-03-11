from flask import Flask, render_template, request, redirect, url_for, session, make_response
import os
import sqlite3
from functools import wraps
from contextlib import contextmanager

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambia-esto-en-produccion')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# Ruta simple para favicon - evita error 500
@app.route('/favicon.ico')
def favicon():
    return '', 204

# DEBUG: ver variables de entorno (temporal)
@app.route('/debug')
def debug_env():
    env_vars = {k: v for k, v in os.environ.items() if 'POSTGRES' in k or 'DATABASE' in k or 'STORAGE' in k}
    return {
        'USE_PG': USE_PG,
        'DATABASE_URL': DATABASE_URL[:50] + '...' if DATABASE_URL else None,
        'postgres_vars': env_vars
    }

# Detecta si estamos en producción (PostgreSQL) o local (SQLite)
# Vercel Postgres crea: POSTGRES_URL, POSTGRES_USER, POSTGRES_HOST, etc.
# También puede crear: DATABASE_URL o STORAGE_URL
DATABASE_URL = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL') or os.environ.get('STORAGE_URL')

# Si tenemos componentes de Postgres, construimos la URL
POSTGRES_HOST = os.environ.get('POSTGRES_HOST')
POSTGRES_USER = os.environ.get('POSTGRES_USER')
POSTGRES_PASSWORD = os.environ.get('POSTGRES_PASSWORD')
POSTGRES_DB = os.environ.get('POSTGRES_DB')

if not DATABASE_URL and POSTGRES_HOST and POSTGRES_USER:
    DATABASE_URL = f"postgres://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}/{POSTGRES_DB}"

USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras


def q(sql):
    """Convierte placeholders ? a %s para PostgreSQL."""
    return sql.replace('?', '%s') if USE_PG else sql


@contextmanager
def get_db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = sqlite3.connect('encuesta.db')
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cur = conn.cursor() if USE_PG else conn
        if USE_PG:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS players (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS matches (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    opponent TEXT NOT NULL,
                    is_open INTEGER DEFAULT 0
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS votes (
                    id SERIAL PRIMARY KEY,
                    match_id INTEGER NOT NULL REFERENCES matches(id),
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    voter_ip TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(match_id, voter_ip)
                )
            ''')
        else:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    opponent TEXT NOT NULL,
                    is_open INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    voter_ip TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (match_id) REFERENCES matches(id),
                    FOREIGN KEY (player_id) REFERENCES players(id),
                    UNIQUE(match_id, voter_ip)
                );
            ''')


def fetchone(conn, sql, params=()):
    cur = conn.cursor() if USE_PG else conn
    if USE_PG:
        cur.execute(q(sql), params)
        return cur.fetchone()
    return conn.execute(q(sql), params).fetchone()


def fetchall(conn, sql, params=()):
    cur = conn.cursor() if USE_PG else conn
    if USE_PG:
        cur.execute(q(sql), params)
        return cur.fetchall()
    return conn.execute(q(sql), params).fetchall()


def execute(conn, sql, params=()):
    cur = conn.cursor() if USE_PG else conn
    if USE_PG:
        cur.execute(q(sql), params)
    else:
        conn.execute(q(sql), params)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# ── VOTACIÓN ──────────────────────────────────────────────

@app.route('/')
def index():
    voter_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    with get_db() as conn:
        match = fetchone(conn, 'SELECT * FROM matches WHERE is_open = 1 ORDER BY id DESC LIMIT 1')
        if not match:
            return render_template('index.html', match=None)

        players = fetchall(conn, 'SELECT * FROM players ORDER BY name')
        already_voted = fetchone(conn,
            'SELECT player_id FROM votes WHERE match_id = ? AND voter_ip = ?',
            (match['id'], voter_ip))

        voted_player = None
        if already_voted:
            voted_player = fetchone(conn,
                'SELECT name FROM players WHERE id = ?',
                (already_voted['player_id'],))

    return render_template('index.html', match=match, players=players,
                           already_voted=already_voted, voted_player=voted_player)


@app.route('/votar', methods=['POST'])
def votar():
    player_id = request.form.get('player_id')
    voter_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    with get_db() as conn:
        match = fetchone(conn, 'SELECT * FROM matches WHERE is_open = 1 ORDER BY id DESC LIMIT 1')
        if not match or not player_id:
            return redirect(url_for('index'))
        try:
            execute(conn, 'INSERT INTO votes (match_id, player_id, voter_ip) VALUES (?, ?, ?)',
                    (match['id'], player_id, voter_ip))
        except Exception:
            pass  # ya votó (UNIQUE constraint)
    return redirect(url_for('index'))


@app.route('/ranking')
def ranking():
    with get_db() as conn:
        players = fetchall(conn, '''
            SELECT p.name, COUNT(v.id) as total_votos
            FROM players p
            LEFT JOIN votes v ON p.id = v.player_id
            GROUP BY p.id, p.name
            ORDER BY total_votos DESC
        ''')
        row = fetchone(conn, 'SELECT COUNT(*) as c FROM matches')
        total_partidos = row['c'] if row else 0
    return render_template('ranking.html', players=players, total_partidos=total_partidos)


# ── ADMIN ─────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_panel'))
        error = 'Contraseña incorrecta'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('index'))


@app.route('/admin')
@admin_required
def admin_panel():
    with get_db() as conn:
        players = fetchall(conn, 'SELECT * FROM players ORDER BY name')
        matches = fetchall(conn, 'SELECT * FROM matches ORDER BY id DESC')
        open_match = fetchone(conn, 'SELECT * FROM matches WHERE is_open = 1')

        match_stats = []
        for m in matches:
            votes = fetchall(conn, '''
                SELECT p.name, COUNT(v.id) as votos
                FROM votes v JOIN players p ON v.player_id = p.id
                WHERE v.match_id = ?
                GROUP BY p.id, p.name ORDER BY votos DESC
            ''', (m['id'],))
            total = sum(v['votos'] for v in votes)
            match_stats.append({'match': m, 'votes': votes, 'total': total})

    return render_template('admin.html', players=players,
                           match_stats=match_stats, open_match=open_match)


@app.route('/admin/jugador/add', methods=['POST'])
@admin_required
def add_player():
    name = request.form.get('name', '').strip()
    if name:
        with get_db() as conn:
            try:
                execute(conn, 'INSERT INTO players (name) VALUES (?)', (name,))
            except Exception:
                pass
    return redirect(url_for('admin_panel'))


@app.route('/admin/jugador/delete/<int:player_id>', methods=['POST'])
@admin_required
def delete_player(player_id):
    with get_db() as conn:
        execute(conn, 'DELETE FROM players WHERE id = ?', (player_id,))
    return redirect(url_for('admin_panel'))


@app.route('/admin/partido/new', methods=['POST'])
@admin_required
def new_match():
    opponent = request.form.get('opponent', '').strip()
    date = request.form.get('date', '').strip()
    if opponent and date:
        with get_db() as conn:
            execute(conn, 'UPDATE matches SET is_open = 0')
            execute(conn, 'INSERT INTO matches (date, opponent, is_open) VALUES (?, ?, 1)',
                    (date, opponent))
    return redirect(url_for('admin_panel'))


@app.route('/admin/partido/<int:match_id>/toggle', methods=['POST'])
@admin_required
def toggle_match(match_id):
    with get_db() as conn:
        match = fetchone(conn, 'SELECT * FROM matches WHERE id = ?', (match_id,))
        if match:
            if match['is_open']:
                execute(conn, 'UPDATE matches SET is_open = 0 WHERE id = ?', (match_id,))
            else:
                execute(conn, 'UPDATE matches SET is_open = 0')
                execute(conn, 'UPDATE matches SET is_open = 1 WHERE id = ?', (match_id,))
    return redirect(url_for('admin_panel'))


# Error handler para problemas de base de datos
@app.errorhandler(500)
def handle_db_error(e):
    return render_template('index.html', match=None, db_error=True), 500


# Inicializar BD al arrancar (funciona en local y en Vercel serverless)
try:
    print(f"Inicializando BD... USE_PG={USE_PG}")
    init_db()
    print("BD inicializada OK")
except Exception as e:
    print(f"Error inicializando BD: {e}")
    import traceback
    traceback.print_exc()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
