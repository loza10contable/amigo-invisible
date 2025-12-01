import os
import uuid
import datetime
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import (
    Flask, render_template, request, redirect, url_for,
    abort, flash, g
)
from sqlalchemy import (
    create_engine, Column, Integer, String,
    DateTime, ForeignKey, Boolean
)
from sqlalchemy.orm import (
    sessionmaker, declarative_base, relationship,
    scoped_session
)
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================

load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY", "admin")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///amigo_invisible.db")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super_secret_key")

# =========================
# DB SETUP
# =========================

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine))
Base = declarative_base()


class Participant(Base):
    __tablename__ = "participants"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)

    # relación donde el participante ES quien regala
    given_assignment = relationship(
        "Assignment",
        back_populates="giver",
        foreign_keys="Assignment.giver_id",
        uselist=False,
        cascade="all, delete-orphan"
    )

    # relación donde el participante ES quien recibe
    received_assignment = relationship(
        "Assignment",
        back_populates="receiver",
        foreign_keys="Assignment.receiver_id",
        uselist=False
    )



class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True)

    giver_id = Column(
        Integer, ForeignKey("participants.id"), nullable=False
    )
    receiver_id = Column(
        Integer, ForeignKey("participants.id"), nullable=False
    )

    token = Column(String, unique=True, nullable=False)
    first_seen_at = Column(DateTime, nullable=True)
    viewed = Column(Boolean, default=False)

    giver = relationship(
        "Participant",
        foreign_keys=[giver_id],
        back_populates="given_assignment"
    )

    receiver = relationship(
        "Participant",
        foreign_keys=[receiver_id],
        back_populates="received_assignment"
    )



Base.metadata.create_all(engine)


@app.before_request
def create_session():
    g.db = SessionLocal()


@app.teardown_appcontext
def shutdown_session(exception=None):
    SessionLocal.remove()

# =========================
# HELPERS
# =========================


def require_admin():
    key = request.args.get("key")
    if key != ADMIN_KEY:
        abort(403)


def do_draw(db):
    participants = db.query(Participant).all()
    n = len(participants)
    if n < 2:
        raise ValueError("Se necesitan al menos 2 participantes.")

    ids = [p.id for p in participants]

    # limpiamos sorteo previo
    db.query(Assignment).delete()

    # algoritmo: buscar permutación sin puntos fijos
    max_intentos = 1000
    intentos = 0
    ok = False
    while intentos < max_intentos and not ok:
        receivers = ids[:]
        random.shuffle(receivers)
        if all(g != r for g, r in zip(ids, receivers)):
            ok = True
        intentos += 1

    if not ok:
        raise RuntimeError("No se pudo generar un sorteo válido, intentá de nuevo.")

    for giver_id, receiver_id in zip(ids, receivers):
        token = str(uuid.uuid4())
        a = Assignment(
            giver_id=giver_id,
            receiver_id=receiver_id,
            token=token
        )
        db.add(a)

    db.commit()


def send_email(to_email, subject, html_body):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("GMAIL no configurado, no se envía a", to_email)
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    msg["Subject"] = subject

    part = MIMEText(html_body, "html", "utf-8")
    msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())

# =========================
# RUTAS ADMIN
# =========================


@app.route("/admin")
def admin_panel():
    require_admin()
    db = g.db
    participants = db.query(Participant).all()
    assignments = db.query(Assignment).all()
    assign_map = {a.giver_id: a for a in assignments}
    return render_template("admin.html",
                           participants=participants,
                           assign_map=assign_map)


@app.route("/admin/add", methods=["POST"])
def admin_add_participant():
    require_admin()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()

    if not name or not email:
        flash("Nombre y email son obligatorios", "error")
        return redirect(url_for("admin_panel", key=ADMIN_KEY))

    p = Participant(name=name, email=email)
    g.db.add(p)
    g.db.commit()
    flash("Participante agregado", "success")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


@app.route("/admin/delete/<int:pid>", methods=["POST"])
def admin_delete_participant(pid):
    require_admin()
    db = g.db

    # 1️⃣ borrar cualquier sorteo existente
    db.query(Assignment).delete()

    # 2️⃣ borrar el participante
    p = db.query(Participant).get(pid)
    if p:
        db.delete(p)

    db.commit()
    flash("Participante eliminado. El sorteo fue reiniciado.", "success")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))



@app.route("/admin/draw", methods=["POST"])
def admin_draw():
    require_admin()
    db = g.db
    try:
        do_draw(db)
        flash("Sorteo generado correctamente.", "success")
    except Exception as e:
        flash(f"Error en el sorteo: {e}", "error")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


@app.route("/admin/send", methods=["POST"])
def admin_send():
    require_admin()
    db = g.db
    assignments = db.query(Assignment).all()
    base_url = request.url_root.rstrip("/")

    enviados = 0
    for a in assignments:
        giver = a.giver
        if not giver or not giver.email:
            continue

        link = f"{base_url}{url_for('reveal', token=a.token)}"

        html_body = f'''
        <html>
        <body style="font-family:Arial; background:#f5f5f5; padding:20px;">
          <div style="max-width:520px;margin:auto;background:white;
                      padding:25px;border-radius:10px;">
            <h2 style="text-align:center;">🎄 Amigo Invisible 🎄</h2>
            <p>Hola <b>{giver.name}</b>,</p>
            <p>Papá Noel ya hizo el sorteo. Tu misión secreta está lista 🎁</p>
            <div style="text-align:center;margin:30px 0;">
              <a href="{link}" style="
                background:#e53935;color:white;
                padding:14px 24px;text-decoration:none;
                font-weight:bold;border-radius:6px;">
                Descubrir mi amigo invisible
              </a>
            </div>
            <p style="font-size:12px;color:#777;text-align:center;">
              No compartas este enlace. Podés volver a abrirlo cuando quieras.
            </p>
          </div>
        </body>
        </html>
        '''
        try:
            send_email(giver.email, "🎁 Tu Amigo Invisible ya fue asignado", html_body)
            enviados += 1
        except Exception as e:
            print("Error enviando email a", giver.email, e)

    flash(f"Emails enviados: {enviados}", "success")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))

# =========================
# RUTAS PARTICIPANTE
# =========================


@app.route("/revelar/<token>")
def reveal(token):
    db = g.db
    a = db.query(Assignment).filter_by(token=token).first()
    if not a:
        abort(404)

    if not a.viewed:
        a.viewed = True
        a.first_seen_at = datetime.datetime.now()
        db.commit()

    giver = a.giver
    receiver = a.receiver

    return render_template(
        "reveal.html",
        giver_name=giver.name if giver else "Participante",
        receiver_name=receiver.name if receiver else "???",
        first_seen_at=a.first_seen_at
    )


@app.route("/")
def index():
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


if __name__ == "__main__":
    app.run()


