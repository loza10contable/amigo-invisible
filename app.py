import os
import uuid
import datetime
import random
import smtplib

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import (
    Flask, render_template, request, redirect,
    url_for, abort, flash, g
)

from sqlalchemy import (
    create_engine, Column, Integer, String,
    DateTime, ForeignKey, Boolean, text
)

from sqlalchemy.orm import (
    sessionmaker, declarative_base,
    relationship, scoped_session
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

    # RELACIONES
    given_assignment = relationship(
        "Assignment",
        back_populates="giver",
        foreign_keys="Assignment.giver_id",
        uselist=False,
        cascade="all, delete-orphan"
    )

    received_assignment = relationship(
        "Assignment",
        back_populates="receiver",
        foreign_keys="Assignment.receiver_id",
        uselist=False
    )

    # ✅ WISHLIST
    favorite_color = Column(String)
    shirt_size = Column(String)
    pants_size = Column(String)
    shoe_size = Column(String)
    gift_notes = Column(String)
    wishlist_updated_at = Column(DateTime)


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True)
    giver_id = Column(Integer, ForeignKey("participants.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("participants.id"), nullable=False)

    token = Column(String, unique=True, nullable=False)
    first_seen_at = Column(DateTime)
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

# =========================
# MIGRACIÓN WISHLIST (SQLite)
# =========================

def ensure_wishlist_columns():
    with engine.connect() as conn:
        cols = conn.execute(
            text("PRAGMA table_info(participants)")
        ).fetchall()
        names = {c[1] for c in cols}

        def add(col):
            conn.execute(
                text(f"ALTER TABLE participants ADD COLUMN {col} TEXT")
            )

        for col in [
            "favorite_color", "shirt_size",
            "pants_size", "shoe_size",
            "gift_notes", "wishlist_updated_at"
        ]:
            if col not in names:
                add(col)


ensure_wishlist_columns()

# =========================
# SESSION HANDLING
# =========================

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
    if request.args.get("key") != ADMIN_KEY:
        abort(403)


def do_draw(db):
    participants = db.query(Participant).all()
    if len(participants) < 2:
        raise ValueError("Se necesitan al menos 2 participantes.")

    ids = [p.id for p in participants]
    db.query(Assignment).delete()

    for _ in range(1000):
        receivers = ids[:]
        random.shuffle(receivers)
        if all(g != r for g, r in zip(ids, receivers)):
            break
    else:
        raise RuntimeError("No se pudo generar un sorteo válido")

    for g_id, r_id in zip(ids, receivers):
        db.add(Assignment(
            giver_id=g_id,
            receiver_id=r_id,
            token=str(uuid.uuid4())
        ))

    db.commit()


def send_email(to_email, subject, html):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())

# =========================
# ADMIN
# =========================

@app.route("/admin")
def admin_panel():
    require_admin()
    db = g.db
    participants = db.query(Participant).all()
    assignments = db.query(Assignment).all()
    assign_map = {a.giver_id: a for a in assignments}
    return render_template(
        "admin.html",
        participants=participants,
        assign_map=assign_map
        ADMIN_KEY=ADMIN_KEY   # ← AGREGAR
    )


@app.route("/admin/add", methods=["POST"])
def admin_add():
    require_admin()
    name = request.form.get("name")
    email = request.form.get("email")
    if not name or not email:
        flash("Nombre y email obligatorios", "error")
    else:
        g.db.add(Participant(name=name, email=email))
        g.db.commit()
        flash("Participante agregado", "success")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


@app.route("/admin/delete/<int:pid>", methods=["POST"])
def admin_delete(pid):
    require_admin()
    g.db.query(Assignment).delete()
    p = g.db.query(Participant).get(pid)
    if p:
        g.db.delete(p)
    g.db.commit()
    flash("Participante eliminado. Sorteo reiniciado.", "success")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


@app.route("/admin/draw", methods=["POST"])
def admin_draw():
    require_admin()
    try:
        do_draw(g.db)
        flash("Sorteo generado", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


@app.route("/admin/send", methods=["POST"])
def admin_send():
    require_admin()
    assignments = g.db.query(Assignment).all()
    base = request.url_root.rstrip("/")
    enviados = 0

    for a in assignments:
        giver = a.giver
        link = f"{base}{url_for('reveal', token=a.token)}"
        send_email(
            giver.email,
            "🎁 Tu Amigo Invisible",
            f"<p>Hola {giver.name}</p><a href='{link}'>Descubrir</a>"
        )
        enviados += 1

    flash(f"Emails enviados: {enviados}", "success")
    return redirect(url_for("admin_panel", key=ADMIN_KEY))

# =========================
# PARTICIPANTE
# =========================

@app.route("/revelar/<token>", methods=["GET", "POST"])
def reveal(token):
    db = g.db
    a = db.query(Assignment).filter_by(token=token).first()
    if not a:
        abort(404)

    receiver = a.receiver
    if not receiver:
        abort(404)


    if request.method == "POST":
        receiver.favorite_color = request.form.get("favorite_color")
        receiver.shirt_size = request.form.get("shirt_size")
        receiver.pants_size = request.form.get("pants_size")
        receiver.shoe_size = request.form.get("shoe_size")
        receiver.gift_notes = request.form.get("gift_notes")
        receiver.wishlist_updated_at = datetime.datetime.now()
        db.commit()
        flash("🎁 Preferencias guardadas", "success")

    if not a.viewed:
        a.viewed = True
        a.first_seen_at = datetime.datetime.now()
        db.commit()

    return render_template(
        "reveal.html",
        receiver_name=receiver.name,
        wishlist=receiver
    )


@app.route("/")
def index():
    return redirect(url_for("admin_panel", key=ADMIN_KEY))


if __name__ == "__main__":
    app.run()

