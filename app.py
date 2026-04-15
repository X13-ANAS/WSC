import sqlite3
import os
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super_secret_key_123' 
socketio = SocketIO(app, cors_allowed_origins="*")

app.config['UPLOAD_FOLDER'] = 'static/uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True) 

online_users = {}

def init_db():
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, recipient TEXT, message TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS custom_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, members TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (id INTEGER PRIMARY KEY AUTOINCREMENT, blocker TEXT, blocked TEXT)''')
        conn.commit()

init_db()

# --- Serve Service Worker for Mobile Notifications ---
@app.route('/sw.js')
def sw():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        hashed_pw = generate_password_hash(password) 
        try:
            with sqlite3.connect('chat.db') as conn:
                c = conn.cursor()
                c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
                conn.commit()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash("Username already exists! Try another.")
            return redirect(url_for('register'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        with sqlite3.connect('chat.db') as conn:
            c = conn.cursor()
            c.execute("SELECT password FROM users WHERE username=?", (username,))
            user = c.fetchone()
        
        if user and check_password_hash(user[0], password):
            session['username'] = username 
            return redirect(url_for('index'))
        else:
            flash("Invalid Username or Password!")
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None) 
    return redirect(url_for('login'))

@app.route('/')
def index():
    if 'username' not in session: return redirect(url_for('login'))
    return render_template('index.html', my_name=session['username'])

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return "No file", 400
    file = request.files['file']
    if file.filename == '': return "No file selected", 400
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return f"/{filepath}|{filename}", 200

@app.route('/block_user', methods=['POST'])
def block_user():
    blocker = session.get('username')
    blocked = request.form.get('target')
    if not blocker or not blocked: return "Error", 400
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute("INSERT INTO blocked_users (blocker, blocked) VALUES (?, ?)", (blocker, blocked))
        conn.commit()
    return "OK", 200

@app.route('/unblock_user', methods=['POST'])
def unblock_user():
    blocker = session.get('username')
    blocked = request.form.get('target')
    if not blocker or not blocked: return "Error", 400
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute("DELETE FROM blocked_users WHERE blocker=? AND blocked=?", (blocker, blocked))
        conn.commit()
    return "OK", 200

@app.route('/leave_group', methods=['POST'])
def leave_group():
    username = session.get('username')
    group_id_str = request.form.get('group_id')
    if not username or not group_id_str: return "Error", 400
    
    group_id = group_id_str.replace("GROUP_", "")
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute("SELECT members FROM custom_groups WHERE id=?", (group_id,))
        row = c.fetchone()
        if row:
            members = row[0].split(',')
            if username in members:
                members.remove(username)
                if len(members) > 0:
                    c.execute("UPDATE custom_groups SET members=? WHERE id=?", (",".join(members), group_id))
                else:
                    c.execute("DELETE FROM custom_groups WHERE id=?", (group_id,))
            conn.commit()
    return "OK", 200

@socketio.on('register')
def handle_register(username):
    online_users[username] = request.sid
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute("SELECT blocked FROM blocked_users WHERE blocker=?", (username,))
        blocked_by_me = [row[0] for row in c.fetchall()]

        c.execute("SELECT username FROM users")
        all_users = [row[0] for row in c.fetchall()]

        c.execute("SELECT id, name, members FROM custom_groups")
        my_groups = []
        for g in c.fetchall():
            member_list = g[2].split(',')
            if username in member_list:
                my_groups.append({'id': f"GROUP_{g[0]}", 'name': g[1], 'members': member_list})

    emit('update_users', {'contacts': all_users, 'online': list(online_users.keys()), 'groups': my_groups, 'blocked': blocked_by_me}, broadcast=True)

    group_ids = [g['id'] for g in my_groups]
    query = '''SELECT id, sender, recipient, message FROM messages 
               WHERE (recipient = "" OR recipient IS NULL OR recipient = ? OR sender = ?)'''
    params = [username, username]
    if group_ids:
        placeholders = ','.join(['?'] * len(group_ids))
        query += f" OR recipient IN ({placeholders})"
        params.extend(group_ids)
        
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute(query, params)
        history = [{'id': row[0], 'user': row[1], 'recipient': row[2] if row[2] else None, 'message': row[3]} for row in c.fetchall()]
    
    emit('load_history', history, room=request.sid)

@socketio.on('create_group')
def handle_create_group(data):
    name = data['name']
    members = ",".join(data['members'])
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute("INSERT INTO custom_groups (name, members) VALUES (?, ?)", (name, members))
        group_id = c.lastrowid
        conn.commit()
    
    group_data = {'id': f"GROUP_{group_id}", 'name': name, 'members': data['members']}
    for member in data['members']:
        if member in online_users:
            emit('group_added', group_data, room=online_users[member])

@socketio.on('disconnect')
def handle_disconnect():
    for user, sid in list(online_users.items()):
        if sid == request.sid:
            del online_users[user]
            with sqlite3.connect('chat.db') as conn:
                c = conn.cursor()
                c.execute("SELECT username FROM users")
                all_users = [row[0] for row in c.fetchall()]
            emit('update_users', {'contacts': all_users, 'online': list(online_users.keys()), 'groups': [], 'blocked': []}, broadcast=True)
            break

@socketio.on('send_message')
def handle_message(data):
    sender = data.get('user')
    recipient = data.get('recipient') or "" 
    message = data.get('message')

    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        if recipient and not recipient.startswith("GROUP_"):
            c.execute("SELECT 1 FROM blocked_users WHERE blocker=? AND blocked=?", (recipient, sender))
            if c.fetchone(): return 

        c.execute("INSERT INTO messages (sender, recipient, message) VALUES (?, ?, ?)", (sender, recipient, message))
        msg_id = c.lastrowid 
        conn.commit()
        data['id'] = msg_id 

        if recipient.startswith("GROUP_"):
            group_id = recipient.split("_")[1]
            c.execute("SELECT members FROM custom_groups WHERE id=?", (group_id,))
            res = c.fetchone()
            if res:
                members = res[0].split(',')
                for member in members:
                    if member in online_users:
                        emit('receive_message', data, room=online_users[member])
        elif recipient and recipient in online_users:
            emit('receive_message', data, room=online_users[recipient])
            emit('receive_message', data, room=request.sid)
        elif not recipient:
            emit('receive_message', data, broadcast=True)

@socketio.on('delete_message')
def handle_delete(msg_id):
    with sqlite3.connect('chat.db') as conn:
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE id=?", (msg_id,))
        conn.commit()
    emit('message_deleted', msg_id, broadcast=True)

# --- WebRTC Signaling Routes ---
@socketio.on('webrtc_offer')
def handle_offer(data):
    if data['recipient'] in online_users: emit('webrtc_offer', data, room=online_users[data['recipient']])

@socketio.on('webrtc_answer')
def handle_answer(data):
    if data['recipient'] in online_users: emit('webrtc_answer', data, room=online_users[data['recipient']])

@socketio.on('webrtc_ice_candidate')
def handle_ice_candidate(data):
    if data['recipient'] in online_users: emit('webrtc_ice_candidate', data, room=online_users[data['recipient']])

@socketio.on('end_call')
def handle_end_call(data):
    if data['recipient'] in online_users: emit('call_ended', data, room=online_users[data['recipient']])

@socketio.on('reject_call')
def handle_reject_call(data):
    if data['sender'] in online_users: emit('call_rejected', data, room=online_users[data['sender']])

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)