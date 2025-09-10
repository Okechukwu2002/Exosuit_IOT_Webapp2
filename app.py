
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import threading, time, collections, os

# ----- Config -----
APP_PORT = 5000
DB_PATH = "exosuit_auth.db"
HISTORY_LEN = 240

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('EXOSUIT_SECRET', 'change_this_secret_in_prod')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
CORS(app)
db = SQLAlchemy(app)
lock = threading.Lock()

# ----- Models -----
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(300), nullable=False)
    role = db.Column(db.String(30), nullable=False, default='patient')
    created_at = db.Column(db.DateTime, default=db.func.now())

class SensorData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.Integer)  # epoch ms
    emg = db.Column(db.Float)
    accel_x = db.Column(db.Float)
    accel_y = db.Column(db.Float)
    accel_z = db.Column(db.Float)
    gyro_x = db.Column(db.Float)
    gyro_y = db.Column(db.Float)
    gyro_z = db.Column(db.Float)

class TherapistNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.Integer)
    author = db.Column(db.String(120))
    note = db.Column(db.Text)

class CommandState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    motor1 = db.Column(db.Integer, default=0)
    motor2 = db.Column(db.Integer, default=0)
    motor3 = db.Column(db.Integer, default=0)
    motor4 = db.Column(db.Integer, default=0)
    motor5 = db.Column(db.Integer, default=0)
    motor6 = db.Column(db.Integer, default=0)

# Ensure one CommandState row
def ensure_command_row():
    with app.app_context():
        if CommandState.query.first() is None:
            db.session.add(CommandState())
            db.session.commit()

# ----- Helpers -----
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# In-memory history for fast dashboard
sensor_history = collections.deque(maxlen=HISTORY_LEN)

# Thresholds for alerts
THRESHOLDS = {"emg": 900, "accel": 8.0, "gyro": 200.0}

# ----- Routes: Auth -----
@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        email = request.form.get('email','').lower().strip()
        password = request.form.get('password','')
        role = request.form.get('role','patient')
        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for('register'))
        user = User(username=username, email=email, password=generate_password_hash(password), role=role)
        db.session.add(user)
        db.session.commit()
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for('login'))
    return render_template_string(REGISTER_HTML)

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email','').lower().strip()
        password = request.form.get('password','')
        u = User.query.filter_by(email=email).first()
        if u and check_password_hash(u.password, password):
            session['user_id'] = u.id
            session['username'] = u.username
            session['role'] = u.role
            flash(f"Welcome, {u.username} ({u.role})", "success")
            return redirect(url_for('index'))
        flash("Invalid credentials.", "danger")
        return redirect(url_for('login'))
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for('login'))

# ----- Main dashboard -----
@app.route('/')
@login_required
def index():
    # prepare commands dict guaranteed
    cs = CommandState.query.first()
    if cs:
        commands = {
            "motor1": cs.motor1, "motor2": cs.motor2, "motor3": cs.motor3,
            "motor4": cs.motor4, "motor5": cs.motor5, "motor6": cs.motor6
        }
    else:
        # fallback zeros
        commands = {f"motor{i}":0 for i in range(1,7)}

    return render_template_string(INDEX_HTML,
                                  thresholds=THRESHOLDS,
                                  commands=commands,
                                  username=session.get('username','User'),
                                  role=session.get('role','patient'))

# ----- Device & dashboard APIs -----
@app.route('/update_data', methods=['POST'])
def update_data():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error":"invalid json"}), 400
    sample = {
        "ts": int(time.time()*1000),
        "emg": float(data.get("emg",0) or 0),
        "accel_x": float(data.get("accel_x",0) or 0),
        "accel_y": float(data.get("accel_y",0) or 0),
        "accel_z": float(data.get("accel_z",0) or 0),
        "gyro_x": float(data.get("gyro_x",0) or 0),
        "gyro_y": float(data.get("gyro_y",0) or 0),
        "gyro_z": float(data.get("gyro_z",0) or 0)
    }
    with lock:
        sensor_history.append(sample)
        sd = SensorData(ts=sample['ts'], emg=sample['emg'], accel_x=sample['accel_x'], accel_y=sample['accel_y'],
                        accel_z=sample['accel_z'], gyro_x=sample['gyro_x'], gyro_y=sample['gyro_y'], gyro_z=sample['gyro_z'])
        db.session.add(sd)
        db.session.commit()
    alerts = []
    if sample['emg'] > THRESHOLDS['emg']:
        alerts.append("High EMG")
    if abs(sample['accel_x']) > THRESHOLDS['accel']:
        alerts.append("High Accel X")
    if abs(sample['gyro_y']) > THRESHOLDS['gyro']:
        alerts.append("High Gyro Y")
    return jsonify({"status":"ok","alerts":alerts})

@app.route('/get_data', methods=['GET'])
@login_required
def get_data():
    with lock:
        history = list(sensor_history)
        latest = history[-1] if history else {"ts":int(time.time()*1000),"emg":0,"accel_x":0,"accel_y":0,"accel_z":0,"gyro_x":0,"gyro_y":0,"gyro_z":0}
        cs = CommandState.query.first()
        cmds = {"motor1":cs.motor1,"motor2":cs.motor2,"motor3":cs.motor3,"motor4":cs.motor4,"motor5":cs.motor5,"motor6":cs.motor6} if cs else {f"motor{i}":0 for i in range(1,7)}
        notes_q = TherapistNote.query.order_by(TherapistNote.ts.desc()).limit(10).all()
        notes_out = [{"ts":n.ts,"author":n.author,"note":n.note} for n in notes_q]
    alerts=[]
    if latest.get("emg",0) > THRESHOLDS['emg']: alerts.append("High EMG")
    if abs(latest.get("accel_x",0)) > THRESHOLDS['accel']: alerts.append("High Accel X")
    if abs(latest.get("gyro_y",0)) > THRESHOLDS['gyro']: alerts.append("High Gyro Y")
    return jsonify({"history":history,"latest":latest,"commands":cmds,"notes":notes_out,"alerts":alerts})

@app.route('/get_command', methods=['GET'])
def get_command():
    cs = CommandState.query.first()
    if not cs:
        return jsonify({f"motor{i}":0 for i in range(1,7)})
    return jsonify({"motor1":cs.motor1,"motor2":cs.motor2,"motor3":cs.motor3,"motor4":cs.motor4,"motor5":cs.motor5,"motor6":cs.motor6})

@app.route('/set_command', methods=['POST'])
@login_required
def set_command():
    if session.get('role') != 'therapist':
        return jsonify({"error":"forbidden"}), 403
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error":"no json"}), 400
    cs = CommandState.query.first()
    if not cs:
        cs = CommandState()
        db.session.add(cs)
    changed = False
    if 'motor_all' in data:
        try:
            a = int(float(data['motor_all']))
            a = max(0,min(180,a))
            cs.motor1 = cs.motor2 = cs.motor3 = cs.motor4 = cs.motor5 = cs.motor6 = a
            changed = True
        except:
            pass
    for i in range(1,7):
        key = f"motor{i}"
        if key in data:
            try:
                a = int(float(data[key]))
                a = max(0,min(180,a))
                setattr(cs, key, a)
                changed = True
            except:
                pass
    if changed:
        db.session.commit()
    return jsonify({"status":"ok","commands":{"motor1":cs.motor1,"motor2":cs.motor2,"motor3":cs.motor3,"motor4":cs.motor4,"motor5":cs.motor5,"motor6":cs.motor6}})

@app.route('/save_note', methods=['POST'])
@login_required
def save_note():
    if session.get('role') != 'therapist':
        return jsonify({"error":"forbidden"}), 403
    data = request.get_json(force=True, silent=True)
    if not data or 'note' not in data:
        return jsonify({"error":"missing note"}), 400
    author = session.get('username','therapist')
    text = data.get('note','').strip()
    if not text:
        return jsonify({"error":"empty note"}), 400
    n = TherapistNote(ts=int(time.time()*1000), author=author, note=text)
    db.session.add(n)
    db.session.commit()
    return jsonify({"status":"saved","note":{"ts":n.ts,"author":n.author,"note":n.note}})

@app.route('/get_notes', methods=['GET'])
@login_required
def get_notes():
    notes = TherapistNote.query.order_by(TherapistNote.ts.desc()).limit(50).all()
    return jsonify([{"ts":n.ts,"author":n.author,"note":n.note} for n in notes])

# ----- Templates -----
LOGIN_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>body{background:#071124;color:#fff} .card{margin-top:6rem;}</style>
</head><body>
<div class="container"><div class="row justify-content-center"><div class="col-md-6">
  <div class="card p-4">
    <h3>Smart Exosuit — Login</h3>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat,msg in messages %}
          <div class="alert alert-{{ 'success' if cat=='success' else 'danger' }}">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method="POST">
      <div class="mb-2"><label class="form-label">Email</label><input name="email" class="form-control" required></div>
      <div class="mb-3"><label class="form-label">Password</label><input name="password" type="password" class="form-control" required></div>
      <button class="btn btn-primary">Login</button>
      <a class="btn btn-link" href="{{ url_for('register') }}">Register</a>
    </form>
  </div>
</div></div></div></body></html>
"""

REGISTER_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Register</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>body{background:#071124;color:#fff} .card{margin-top:4rem;}</style>
</head><body>
<div class="container"><div class="row justify-content-center"><div class="col-md-8">
  <div class="card p-4">
    <h3>Register</h3>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat,msg in messages %}
          <div class="alert alert-{{ 'success' if cat=='success' else 'danger' }}">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method="POST">
      <div class="row">
        <div class="col-md-6 mb-2"><label class="form-label">Full name</label><input name="username" class="form-control" required></div>
        <div class="col-md-6 mb-2"><label class="form-label">Email</label><input name="email" type="email" class="form-control" required></div>
      </div>
      <div class="mb-2"><label class="form-label">Password</label><input name="password" type="password" class="form-control" required></div>
      <div class="mb-3"><label class="form-label">Role</label>
        <select name="role" class="form-select">
          <option value="patient">Patient</option>
          <option value="therapist">Therapist</option>
        </select>
      </div>
      <button class="btn btn-success">Register</button>
      <a class="btn btn-link" href="{{ url_for('login') }}">Login</a>
    </form>
  </div>
</div></div></div></body></html>
"""

# Defensive INDEX_HTML: uses commands | default({}) so Jinja won't raise if missing
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Exosuit Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root{--bg:#071124;--card:#0f1720;--muted:#9aa4b2;--accent:#22c55e}
body{background:linear-gradient(180deg,#071124,#0b1d2b);color:#e6eef6;font-family:Inter,system-ui;}
.container{max-width:1200px;margin-top:20px}
.card{background:linear-gradient(180deg,rgba(255,255,255,0.02),rgba(255,255,255,0.01));border-radius:10px;border:1px solid rgba(255,255,255,0.03)}
.small-muted{color:var(--muted);font-size:.9rem}
.motor-dial{width:80px;height:80px;border-radius:50%;border:6px solid rgba(255,255,255,0.04);position:relative;margin:auto;background:linear-gradient(180deg,rgba(255,255,255,0.01),rgba(255,255,255,0.03))}
.needle{width:4px;height:36px;background:var(--accent);position:absolute;left:calc(50% - 2px);top:12px;transform-origin:bottom center;transform:rotate(-90deg);transition:transform .35s}
.note-box{background:rgba(255,255,255,0.02);padding:10px;border-left:4px solid rgba(34,197,94,0.9);border-radius:6px}
.footer{margin-top:18px;padding:8px;color:var(--muted);text-align:center;font-size:0.9rem}
</style>
</head>
<body>
<div class="container">
  <div class="d-flex justify-content-between mb-3">
    <div><h4>Smart Exosuit Dashboard</h4><div class="small-muted">User: {{ username|default('User') }} • Role: {{ role|default('patient') }}</div></div>
    <div>
      <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm">Logout</a>
    </div>
  </div>

  <div class="row g-3">
    <div class="col-lg-8">
      <div class="card p-3">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <h5 class="mb-0">EMG (Muscle Activity)</h5><div class="small-muted">live</div>
        </div>
        <canvas id="emgChart" style="height:260px"></canvas>
      </div>
    </div>

    <div class="col-lg-4">
      <div class="card p-3">
        <h6>Snapshot</h6>
        <div id="snapshot" class="small-muted mb-2">Waiting for data</div>
        {% if role == 'therapist' %}
        <div class="mb-2"><button id="openNotesBtn" class="btn btn-sm btn-primary">View Notes</button></div>
        {% endif %}
        <h6 class="mb-1">Latest Note</h6>
        <div id="latestNote" class="note-box">No notes yet</div>
      </div>
    </div>
  </div>

  <div class="row g-3 mt-1">
    <div class="col-lg-8">
      <div class="card p-3">
        <h5>IMU — Accel & Gyro</h5>
        <canvas id="imuChart" style="height:200px"></canvas>
      </div>
    </div>

    <div class="col-lg-4">
      <div class="card p-3">
        <h6>Motors</h6>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px">
          {% for i in range(1,7) %}
          <div class="text-center">
            <div class="motor-dial"><div class="needle" id="needle{{ i }}"></div></div>
            <div class="small-muted mt-2">Motor {{ i }}</div>
            <input type="range" min="0" max="180" id="slider{{ i }}" value="{{ (commands|default({})).get('motor' ~ i, 0) }}" class="form-range">
            {% if role == 'therapist' %}
            <button class="btn btn-sm btn-primary w-100 set-btn" data-motor="motor{{ i }}">Send</button>
            {% endif %}
          </div>
          {% endfor %}
        </div>
        {% if role == 'therapist' %}
        <div class="mt-3">
          <label>Set all motors</label>
          <div class="d-flex gap-2">
            <input id="allAngle" class="form-control form-control-sm" type="number" min="0" max="180" placeholder="angle">
            <button id="setAllBtn" class="btn btn-success btn-sm">Set</button>
          </div>
        </div>
        {% endif %}
      </div>
    </div>
  </div>

  <div class="row g-3 mt-2">
    <div class="col-12">
      <div class="card p-3">
        <h6>Therapist Notes & Instructions</h6>
        {% if role == 'therapist' %}
        <div class="d-flex gap-2 mb-2">
          <textarea id="noteInput" class="form-control" rows="3" placeholder="Write note..."></textarea>
          <div style="min-width:150px">
            <input id="authorInput" class="form-control mb-2" placeholder="Author (auto filled)">
            <button id="saveNoteBtn" class="btn btn-success w-100">Save Note</button>
          </div>
        </div>
        {% else %}
        <div class="small-muted mb-2">Patients cannot add notes. Contact your therapist to add instructions.</div>
        {% endif %}
        <div id="notesList" style="max-height:260px;overflow:auto"></div>
      </div>
    </div>
  </div>

  <div class="footer">ESP32 → POST /update_data • ESP32 polls /get_command • Only therapists can set commands & save notes</div>
</div>

<script>
const ROLE = "{{ role|default('patient') }}";
const USER = "{{ username|default('User')|e }}";
const POLL_MS = 800;

const emgCtx = document.getElementById('emgChart').getContext('2d');
const emgChart = new Chart(emgCtx, { type: 'line', data: { labels: [], datasets:[{label:'EMG', borderColor:'#7CFC00', data:[]}]}, options:{animation:false}});

const imuCtx = document.getElementById('imuChart').getContext('2d');
const imuChart = new Chart(imuCtx, { type:'line', data:{ labels:[], datasets:[{label:'Accel X',borderColor:'#00FFFF',data:[]},{label:'Accel Y',borderColor:'#FFA500',data:[]},{label:'Gyro Y',borderColor:'#FF66CC',data:[]}]}, options:{animation:false}});

function pushTrim(chart, vals){
  chart.data.labels.push('');
  chart.data.datasets.forEach((ds,i)=> ds.data.push(vals[i]));
  if(chart.data.labels.length>80){ chart.data.labels.shift(); chart.data.datasets.forEach(ds=>ds.data.shift()); }
  chart.update();
}

function setNeedle(i, angle){
  const n = document.getElementById('needle'+i);
  if(n) n.style.transform = `rotate(${angle-90}deg)`;
}

async function poll(){
  try{
    const r = await fetch('/get_data');
    if(!r.ok) return;
    const j = await r.json();
    const latest = j.latest || {};
    document.getElementById('snapshot').innerText = `EMG: ${Number(latest.emg||0).toFixed(1)} • AccelX: ${Number(latest.accel_x||0).toFixed(2)}`;

    // charts
    emgChart.data.labels.push('');
    emgChart.data.datasets[0].data.push(latest.emg||0);
    if(emgChart.data.labels.length>80){ emgChart.data.labels.shift(); emgChart.data.datasets[0].data.shift(); }
    emgChart.update();

    pushTrim(imuChart, [latest.accel_x||0, latest.accel_y||0, latest.gyro_y||0]);

    // commands animate
    const cmds = j.commands || {};
    for(let i=1;i<=6;i++){
      const a = cmds['motor'+i] || 0;
      setNeedle(i,a);
      const s = document.getElementById('slider'+i);
      if(s && Number(s.value) !== Number(a)) s.value = a;
    }

    // notes
    const notes = j.notes || [];
    document.getElementById('latestNote').innerText = notes.length ? notes[0].note : 'No notes yet';
    renderNotesList(notes);
  }catch(e){ console.error(e); }
}

function renderNotesList(notes){
  const c = document.getElementById('notesList');
  c.innerHTML = '';
  if(!notes.length){ c.innerHTML = '<div class="small-muted">No notes</div>'; return; }
  notes.forEach(n=>{
    const d = new Date(n.ts);
    const el = document.createElement('div');
    el.className='note-box mb-2';
    el.innerHTML = `<div class="small-muted">${n.author} • ${d.toLocaleString()}</div><div>${n.note}</div>`;
    c.appendChild(el);
  });
}

async function sendMotor(motorKey, angle){
  if(ROLE !== 'therapist'){ alert('Only therapists can send commands'); return; }
  await fetch('/set_command', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[motorKey]: angle}) });
}

document.querySelectorAll('.set-btn').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    const motor = btn.getAttribute('data-motor');
    const idx = motor.replace('motor','');
    const angle = document.getElementById('slider'+idx).value;
    sendMotor(motor, parseInt(angle));
  });
});

document.getElementById('setAllBtn')?.addEventListener('click', async ()=>{
  if(ROLE !== 'therapist'){ alert('Only therapists can set all motors'); return; }
  const angle = parseInt(document.getElementById('allAngle').value || 0);
  if(isNaN(angle)) return alert('Enter valid angle 0-180');
  await fetch('/set_command', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({motor_all: angle}) });
});

document.getElementById('saveNoteBtn')?.addEventListener('click', async ()=>{
  if(ROLE !== 'therapist'){ alert('Only therapists can save notes'); return; }
  const note = document.getElementById('noteInput').value.trim();
  if(!note) return alert('Enter note');
  await fetch('/save_note', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({note}) });
  document.getElementById('noteInput').value = '';
  poll();
});

// start polling
poll();
setInterval(poll, POLL_MS);
</script>
</body>
</html>
"""

# ----- Startup -----
with app.app_context():
    db.create_all()
    ensure_command_row()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT, debug=True)
