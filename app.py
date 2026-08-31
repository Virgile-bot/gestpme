from flask import Flask, render_template, request, redirect, session, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import pymysql
import pyotp
import qrcode
import io
import base64
import secrets
from datetime import datetime, timedelta
from flask_mail import Mail, Message
from flask_socketio import SocketIO, emit, join_room
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

import os
from datetime import datetime, timedelta
import fedapay
import fedapay

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cle_locale_dev_a_changer')

# Configuration Flask-Mail (Gmail)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'virgilezossou@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'trjtcytggncuqkh')
app.config['MAIL_DEFAULT_SENDER'] = ('GestPME', os.environ.get('MAIL_USERNAME', 'virgilezossou@gmail.com'))

mail = Mail(app)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')

# Code secret pour l'inscription des admins PME
ADMIN_CODE_SECRET = os.environ.get('ADMIN_CODE_SECRET', 'GESTPME-ADMIN-2026')

# Configuration FedaPay
FEDAPAY_SECRET_KEY = os.environ.get('FEDAPAY_SECRET_KEY', 'sk_sandbox_votre_cle')
FEDAPAY_PUBLIC_KEY = os.environ.get('FEDAPAY_PUBLIC_KEY', 'pk_sandbox_votre_cle')
FEDAPAY_ENV = os.environ.get('FEDAPAY_ENV', 'sandbox')

fedapay.api_key = FEDAPAY_SECRET_KEY
fedapay.api_base = 'https://sandbox-api.fedapay.com' if FEDAPAY_ENV == 'sandbox' else 'https://api.fedapay.com'

# Plans d'abonnement GestPME
PLANS = {
    'starter': {'nom': 'Starter', 'prix': 0, 'description': '1 utilisateur · 50 produits · 100 ventes/mois'},
    'pme': {'nom': 'PME', 'prix': 5000, 'description': '5 utilisateurs · Illimité · Factures PDF'},
    'business': {'nom': 'Business', 'prix': 15000, 'description': '20 utilisateurs · Multi-boutiques · API'},
}


def get_connexion():
    connexion = pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'gestpme'),
        port=int(os.environ.get('DB_PORT', 3306)),
        cursorclass=pymysql.cursors.DictCursor
    )
    return connexion


def get_etat_vente(curseur, vente_id, pme_id):
    """
    Détermine l'état d'une vente pour savoir quelles actions sont permises.
    Retourne 'libre', 'facturee', 'validee_dgi' ou None si la vente n'existe pas.
    """
    curseur.execute("""
        SELECT 
            v.id,
            f.id AS facture_id,
            f.transmise_dgi
        FROM ventes v
        LEFT JOIN factures f ON f.vente_id = v.id
        WHERE v.id = %s AND v.pme_id = %s
    """, (vente_id, pme_id))
    resultat = curseur.fetchone()

    if not resultat:
        return None
    if resultat['facture_id'] is None:
        return 'libre'
    if resultat['transmise_dgi']:
        return 'validee_dgi'
    return 'facturee'


def connexion_requise(fonction):
    @wraps(fonction)
    def fonction_protegee(*args, **kwargs):
        if 'utilisateur_id' not in session:
            return redirect('/login')
        return fonction(*args, **kwargs)

    return fonction_protegee


def dgi_requis(fonction):
    @wraps(fonction)
    def fonction_protegee(*args, **kwargs):
        if 'utilisateur_id' not in session:
            return redirect('/login')
        if session.get('role') != 'superviseur_dgi':
            return "Accès réservé à la DGI", 403
        return fonction(*args, **kwargs)

    return fonction_protegee


def gerant_requis(fonction):
    """
    Protège les routes métier PME (ventes, stocks, factures, dépenses...).
    Un compte superviseur DGI, bien que connecté, n'a pas de pme_id et ne
    doit jamais pouvoir créer/modifier des données métier d'une entreprise.
    """
    @wraps(fonction)
    def fonction_protegee(*args, **kwargs):
        if 'utilisateur_id' not in session:
            return redirect('/login')
        if session.get('role') == 'superviseur_dgi':
            return "Cette action est réservée aux comptes PME, pas à la supervision DGI.", 403
        return fonction(*args, **kwargs)

    return fonction_protegee


def admin_requis(fonction):
    """
    Protège les routes réservées à l'admin PME.
    Seul un compte admin_pme peut accéder au tableau de bord admin,
    valider/rejeter des ventes et générer des rapports.
    """
    @wraps(fonction)
    def fonction_protegee(*args, **kwargs):
        if 'utilisateur_id' not in session:
            return redirect('/login')
        if session.get('role') not in ['admin_pme']:
            return "Accès réservé à l'administrateur de la PME.", 403
        return fonction(*args, **kwargs)

    return fonction_protegee


def super_admin_requis(fonction):
    """
    Protège les routes réservées au super-admin GestPME (fondateur).
    Accès total à toutes les PME de la plateforme.
    """
    @wraps(fonction)
    def fonction_protegee(*args, **kwargs):
        if 'utilisateur_id' not in session:
            return redirect('/login')
        if session.get('role') != 'super_admin':
            return "Accès réservé au super-administrateur GestPME.", 403
        return fonction(*args, **kwargs)

    return fonction_protegee


@app.route('/')
def accueil():
    return render_template('accueil.html')


@app.route('/inscription', methods=['GET', 'POST'])
def inscription():
    if request.method == 'POST':
        nom_entreprise = request.form['nom_entreprise']
        ifu = request.form['ifu']
        telephone = request.form['telephone']
        adresse = request.form['adresse']
        nom_complet = request.form['nom_complet']
        email = request.form['email']
        mot_de_passe = request.form['mot_de_passe']

        connexion = get_connexion()
        curseur = connexion.cursor()

        curseur.execute("SELECT id FROM pme WHERE ifu = %s", (ifu,))
        if curseur.fetchone():
            connexion.close()
            return render_template('inscription.html', erreur="Cet IFU est déjà enregistré")

        curseur.execute("SELECT id FROM utilisateurs WHERE email = %s", (email,))
        if curseur.fetchone():
            connexion.close()
            return render_template('inscription.html', erreur="Cet email est déjà utilisé")

        try:
            curseur.execute("""
                INSERT INTO pme (nom_entreprise, ifu, telephone, adresse)
                VALUES (%s, %s, %s, %s)
            """, (nom_entreprise, ifu, telephone, adresse))
            pme_id = curseur.lastrowid

            mot_de_passe_hash = generate_password_hash(mot_de_passe)
            curseur.execute("""
                INSERT INTO utilisateurs (pme_id, nom_complet, email, mot_de_passe_hash, role)
                VALUES (%s, %s, %s, %s, %s)
            """, (pme_id, nom_complet, email, mot_de_passe_hash, 'gerant'))

            connexion.commit()
            connexion.close()

            return redirect('/login')

        except Exception as erreur:
            connexion.rollback()
            connexion.close()
            return render_template('inscription.html', erreur=f"Erreur lors de l'inscription : {erreur}")

    return render_template('inscription.html', erreur=None)


@app.route('/inscription-admin', methods=['GET', 'POST'])
def inscription_admin():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT id, nom_entreprise FROM pme ORDER BY nom_entreprise ASC")
    pme_liste = curseur.fetchall()
    connexion.close()

    if request.method == 'POST':
        code_saisi = request.form['code_secret']
        pme_id = request.form['pme_id']
        nom_complet = request.form['nom_complet']
        email = request.form['email']
        mot_de_passe = request.form['mot_de_passe']

        # Vérification du code secret
        if code_saisi != ADMIN_CODE_SECRET:
            return render_template('inscription_admin.html',
                                   erreur="Code d'accès incorrect.",
                                   pme_liste=pme_liste)

        connexion = get_connexion()
        curseur = connexion.cursor()

        # Vérifier que l'email n'est pas déjà utilisé
        curseur.execute("SELECT id FROM utilisateurs WHERE email = %s", (email,))
        if curseur.fetchone():
            connexion.close()
            return render_template('inscription_admin.html',
                                   erreur="Cet email est déjà utilisé.",
                                   pme_liste=pme_liste)

        # Vérifier qu'il n'y a pas déjà un admin pour cette PME
        curseur.execute("""
            SELECT id FROM utilisateurs 
            WHERE pme_id = %s AND role = 'admin_pme'
        """, (pme_id,))
        if curseur.fetchone():
            connexion.close()
            return render_template('inscription_admin.html',
                                   erreur="Cette entreprise a déjà un administrateur.",
                                   pme_liste=pme_liste)

        try:
            mot_de_passe_hash = generate_password_hash(mot_de_passe)
            curseur.execute("""
                INSERT INTO utilisateurs (pme_id, nom_complet, email, mot_de_passe_hash, role)
                VALUES (%s, %s, %s, %s, 'admin_pme')
            """, (pme_id, nom_complet, email, mot_de_passe_hash))
            connexion.commit()
            connexion.close()
            return redirect('/login')

        except Exception as erreur:
            connexion.rollback()
            connexion.close()
            return render_template('inscription_admin.html',
                                   erreur=f"Erreur : {erreur}",
                                   pme_liste=pme_liste)

    return render_template('inscription_admin.html', erreur=None, pme_liste=pme_liste)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        mot_de_passe = request.form['mot_de_passe']

        connexion = get_connexion()
        curseur = connexion.cursor()
        curseur.execute("SELECT * FROM utilisateurs WHERE email = %s", (email,))
        utilisateur = curseur.fetchone()
        connexion.close()

        if utilisateur and check_password_hash(utilisateur['mot_de_passe_hash'], mot_de_passe):
            if utilisateur['mfa_active']:
                # MFA activé → stocker temporairement l'ID en session et demander le code
                session['mfa_en_attente_id'] = utilisateur['id']
                session['mfa_en_attente_role'] = utilisateur['role']
                session['mfa_en_attente_nom'] = utilisateur['nom_complet']
                session['mfa_en_attente_pme_id'] = utilisateur['pme_id']
                return redirect('/login/mfa')
            else:
                # Pas de MFA → connexion directe
                session['utilisateur_id'] = utilisateur['id']
                session['nom'] = utilisateur['nom_complet']
                session['role'] = utilisateur['role']
                session['pme_id'] = utilisateur['pme_id']
                if utilisateur['role'] == 'superviseur_dgi':
                    return redirect('/dgi')
                elif utilisateur['role'] == 'admin_pme':
                    return redirect('/admin')
                elif utilisateur['role'] == 'super_admin':
                    return redirect('/super-admin')
                return redirect('/dashboard')
        else:
            return render_template('login.html', erreur="Email ou mot de passe incorrect")

    return render_template('login.html', erreur=None)


@app.route('/login/mfa', methods=['GET', 'POST'])
def login_mfa():
    # Vérifier qu'on vient bien d'une connexion en attente de MFA
    if 'mfa_en_attente_id' not in session:
        return redirect('/login')

    if request.method == 'POST':
        code_saisi = request.form['code_mfa']
        utilisateur_id = session['mfa_en_attente_id']

        connexion = get_connexion()
        curseur = connexion.cursor()
        curseur.execute("SELECT mfa_secret FROM utilisateurs WHERE id = %s", (utilisateur_id,))
        resultat = curseur.fetchone()
        connexion.close()

        totp = pyotp.TOTP(resultat['mfa_secret'])

        if totp.verify(code_saisi):
            # Code correct → ouvrir la vraie session
            session['utilisateur_id'] = session.pop('mfa_en_attente_id')
            session['nom'] = session.pop('mfa_en_attente_nom')
            session['role'] = session.pop('mfa_en_attente_role')
            session['pme_id'] = session.pop('mfa_en_attente_pme_id')

            if session['role'] == 'superviseur_dgi':
                return redirect('/dgi')
            elif session['role'] == 'admin_pme':
                return redirect('/admin')
            elif session['role'] == 'super_admin':
                return redirect('/super-admin')
            return redirect('/dashboard')
        else:
            return render_template('login_mfa.html', erreur="Code incorrect ou expiré. Réessayez.")

    return render_template('login_mfa.html', erreur=None)


@app.route('/dashboard')
@connexion_requise
def dashboard():
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("""
        SELECT 
            COALESCE(SUM(montant_total), 0) AS total_jour,
            COUNT(*) AS nombre_ventes
        FROM ventes
        WHERE pme_id = %s AND DATE(date_vente) = CURDATE() AND statut != 'annulee'
    """, (session['pme_id'],))
    ventes_jour = curseur.fetchone()

    # Ventes du jour précédent, pour comparaison
    curseur.execute("""
        SELECT 
            COALESCE(SUM(montant_total), 0) AS total_veille,
            COUNT(*) AS nombre_ventes_veille
        FROM ventes
        WHERE pme_id = %s AND DATE(date_vente) = DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND statut != 'annulee'
    """, (session['pme_id'],))
    ventes_veille = curseur.fetchone()

    curseur.execute("""
        SELECT COUNT(*) AS nb_critique
        FROM produits
        WHERE pme_id = %s AND actif = TRUE AND quantite_stock <= seuil_alerte
    """, (session['pme_id'],))
    stock_critique = curseur.fetchone()

    connexion.close()

    # Calcul de la variation en pourcentage par rapport à la veille
    total_jour_f = float(ventes_jour['total_jour'])
    total_veille_f = float(ventes_veille['total_veille'])

    if total_veille_f > 0:
        variation_pct = round((total_jour_f - total_veille_f) / total_veille_f * 100, 1)
    else:
        variation_pct = None  # Pas de comparaison possible si la veille était à zéro

    return render_template(
        'dashboard.html',
        nom=session['nom'],
        total_jour=ventes_jour['total_jour'],
        nombre_ventes=ventes_jour['nombre_ventes'],
        nb_critique=stock_critique['nb_critique'],
        total_veille=ventes_veille['total_veille'],
        nombre_ventes_veille=ventes_veille['nombre_ventes_veille'],
        variation_pct=variation_pct
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/mfa/activer')
@gerant_requis
def mfa_activer():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT mfa_active, mfa_secret FROM utilisateurs WHERE id = %s", (session['utilisateur_id'],))
    utilisateur = curseur.fetchone()
    connexion.close()

    if utilisateur['mfa_active']:
        return redirect('/mfa/parametres')

    # Générer un nouveau secret si pas encore fait
    if not utilisateur['mfa_secret']:
        secret = pyotp.random_base32()
        connexion = get_connexion()
        curseur = connexion.cursor()
        curseur.execute("UPDATE utilisateurs SET mfa_secret = %s WHERE id = %s", (secret, session['utilisateur_id']))
        connexion.commit()
        connexion.close()
    else:
        secret = utilisateur['mfa_secret']

    # Générer le QR code
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=session['nom'], issuer_name="GestPME")

    qr = qrcode.make(uri)
    buffer = io.BytesIO()
    qr.save(buffer, format='PNG')
    qr_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

    return render_template('mfa_activer.html', qr_base64=qr_base64, secret=secret)


@app.route('/mfa/confirmer', methods=['POST'])
@gerant_requis
def mfa_confirmer():
    code_saisi = request.form['code_mfa']

    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT mfa_secret FROM utilisateurs WHERE id = %s", (session['utilisateur_id'],))
    resultat = curseur.fetchone()

    totp = pyotp.TOTP(resultat['mfa_secret'])

    if totp.verify(code_saisi):
        curseur.execute("UPDATE utilisateurs SET mfa_active = TRUE WHERE id = %s", (session['utilisateur_id'],))
        connexion.commit()
        connexion.close()
        return redirect('/mfa/parametres')
    else:
        connexion.close()
        return redirect('/mfa/activer')


@app.route('/mfa/desactiver', methods=['POST'])
@gerant_requis
def mfa_desactiver():
    code_saisi = request.form['code_mfa']

    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT mfa_secret FROM utilisateurs WHERE id = %s", (session['utilisateur_id'],))
    resultat = curseur.fetchone()

    totp = pyotp.TOTP(resultat['mfa_secret'])

    if totp.verify(code_saisi):
        curseur.execute(
            "UPDATE utilisateurs SET mfa_active = FALSE, mfa_secret = NULL WHERE id = %s",
            (session['utilisateur_id'],)
        )
        connexion.commit()
        connexion.close()
        return redirect('/mfa/parametres')
    else:
        connexion.close()
        return redirect('/mfa/parametres')


@app.route('/mfa/parametres')
@gerant_requis
def mfa_parametres():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT mfa_active FROM utilisateurs WHERE id = %s", (session['utilisateur_id'],))
    utilisateur = curseur.fetchone()
    connexion.close()

    return render_template('mfa_parametres.html', mfa_active=utilisateur['mfa_active'])


# ============================================
# MODULE VENTES
# ============================================

@app.route('/ventes/nouvelle')
@gerant_requis
def nouvelle_vente():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT * FROM produits WHERE pme_id = %s AND actif = TRUE", (session['pme_id'],))
    produits = curseur.fetchall()
    connexion.close()

    return render_template('nouvelle_vente.html', produits=produits)


@app.route('/ventes/enregistrer', methods=['POST'])
@gerant_requis
def enregistrer_vente():
    client_nom = request.form['client_nom']
    mode_paiement = request.form['mode_paiement']
    produit_id = request.form['produit_id']
    quantite = int(request.form['quantite'])

    if quantite <= 0:
        return "La quantité doit être supérieure à zéro.", 400

    connexion = get_connexion()
    curseur = connexion.cursor()

    try:
        curseur.execute("SELECT * FROM produits WHERE id = %s AND pme_id = %s", (produit_id, session['pme_id']))
        produit = curseur.fetchone()

        if not produit:
            connexion.close()
            return "Produit introuvable", 400

        if produit['quantite_stock'] < quantite:
            connexion.close()
            return "Stock insuffisant pour cette vente", 400

        prix_unitaire = produit['prix_unitaire']
        montant_total = prix_unitaire * quantite

        curseur.execute("""
            INSERT INTO ventes (pme_id, utilisateur_id, client_nom, montant_total, mode_paiement, statut)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (session['pme_id'], session['utilisateur_id'], client_nom, montant_total, mode_paiement, 'payee'))

        vente_id = curseur.lastrowid

        curseur.execute("""
            INSERT INTO lignes_vente (vente_id, produit_id, quantite, prix_unitaire, sous_total)
            VALUES (%s, %s, %s, %s, %s)
        """, (vente_id, produit_id, quantite, prix_unitaire, montant_total))

        stock_avant = produit['quantite_stock']
        stock_apres = stock_avant - quantite

        curseur.execute("""
            UPDATE produits SET quantite_stock = quantite_stock - %s WHERE id = %s
        """, (quantite, produit_id))

        curseur.execute("""
            INSERT INTO mouvements_stock 
                (produit_id, pme_id, type_mouvement, quantite, stock_avant, stock_apres, motif, utilisateur_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            produit_id, session['pme_id'], 'sortie_vente', quantite,
            stock_avant, stock_apres, f"Vente à {client_nom}", session['utilisateur_id']
        ))

        connexion.commit()

        # Sauvegarder la notification pour l'admin
        message_notif = f"Nouvelle vente — {client_nom} · {int(montant_total):,} F · {mode_paiement}".replace(',', ' ')
        connexion2 = get_connexion()
        curseur2 = connexion2.cursor()
        curseur2.execute("""
            INSERT INTO notifications (pme_id, type_notification, message, vente_id)
            VALUES (%s, %s, %s, %s)
        """, (session['pme_id'], 'nouvelle_vente', message_notif, vente_id))
        connexion2.commit()
        connexion2.close()

        # Émettre l'événement WebSocket vers l'admin de cette PME
        socketio.emit('nouvelle_vente', {
            'message': message_notif,
            'vente_id': vente_id,
            'montant': float(montant_total),
            'client': client_nom,
            'produit': produit['nom'],
            'quantite': quantite,
            'heure': datetime.now().strftime('%H:%M:%S')
        }, room=f"admin_pme_{session['pme_id']}")

        connexion.close()
        return redirect('/dashboard')

    except Exception as erreur:
        connexion.rollback()
        connexion.close()
        return f"Erreur lors de l'enregistrement : {erreur}", 500


@app.route('/ventes')
@gerant_requis
def liste_ventes():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT 
            v.id,
            v.client_nom,
            v.montant_total,
            v.mode_paiement,
            v.statut,
            v.date_vente,
            p.nom AS nom_produit,
            lv.quantite,
            f.id AS facture_id,
            f.transmise_dgi
        FROM ventes v
        JOIN lignes_vente lv ON lv.vente_id = v.id
        JOIN produits p ON p.id = lv.produit_id
        LEFT JOIN factures f ON f.vente_id = v.id
        WHERE v.pme_id = %s
        ORDER BY v.date_vente DESC
    """, (session['pme_id'],))
    ventes = curseur.fetchall()
    connexion.close()

    return render_template('liste_ventes.html', ventes=ventes)


@app.route('/ventes/modifier/<int:vente_id>')
@gerant_requis
def modifier_vente(vente_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    etat = get_etat_vente(curseur, vente_id, session['pme_id'])

    if etat is None:
        connexion.close()
        return "Vente introuvable", 404

    if etat != 'libre':
        connexion.close()
        return "Cette vente est déjà facturée et ne peut plus être modifiée. Vous pouvez seulement l'annuler.", 403

    curseur.execute("""
        SELECT v.*, lv.id AS ligne_id, lv.produit_id, lv.quantite
        FROM ventes v
        JOIN lignes_vente lv ON lv.vente_id = v.id
        WHERE v.id = %s AND v.pme_id = %s
    """, (vente_id, session['pme_id']))
    vente = curseur.fetchone()

    curseur.execute("SELECT * FROM produits WHERE pme_id = %s", (session['pme_id'],))
    produits = curseur.fetchall()

    connexion.close()

    return render_template('modifier_vente.html', vente=vente, produits=produits)


@app.route('/ventes/mettre_a_jour/<int:vente_id>', methods=['POST'])
@gerant_requis
def mettre_a_jour_vente(vente_id):
    client_nom = request.form['client_nom']
    mode_paiement = request.form['mode_paiement']
    nouveau_produit_id = request.form['produit_id']
    nouvelle_quantite = int(request.form['quantite'])

    connexion = get_connexion()
    curseur = connexion.cursor()

    try:
        etat = get_etat_vente(curseur, vente_id, session['pme_id'])

        if etat is None:
            connexion.close()
            return "Vente introuvable", 404

        if etat != 'libre':
            connexion.close()
            return "Cette vente est déjà facturée et ne peut plus être modifiée.", 403

        curseur.execute("SELECT * FROM lignes_vente WHERE vente_id = %s", (vente_id,))
        ancienne_ligne = curseur.fetchone()

        curseur.execute(
            "UPDATE produits SET quantite_stock = quantite_stock + %s WHERE id = %s",
            (ancienne_ligne['quantite'], ancienne_ligne['produit_id'])
        )
        curseur.execute("""
            INSERT INTO mouvements_stock 
                (produit_id, pme_id, type_mouvement, quantite, stock_avant, stock_apres, motif, utilisateur_id)
            VALUES (%s, %s, 'correction', %s, 
                    (SELECT quantite_stock FROM produits WHERE id = %s) - %s,
                    (SELECT quantite_stock FROM produits WHERE id = %s),
                    %s, %s)
        """, (
            ancienne_ligne['produit_id'], session['pme_id'], ancienne_ligne['quantite'],
            ancienne_ligne['produit_id'], ancienne_ligne['quantite'],
            ancienne_ligne['produit_id'],
            f"Correction vente #{vente_id} — annulation ancienne ligne", session['utilisateur_id']
        ))

        curseur.execute(
            "SELECT * FROM produits WHERE id = %s AND pme_id = %s",
            (nouveau_produit_id, session['pme_id'])
        )
        nouveau_produit = curseur.fetchone()

        if not nouveau_produit or nouveau_produit['quantite_stock'] < nouvelle_quantite:
            connexion.rollback()
            connexion.close()
            return "Stock insuffisant pour cette modification", 400

        nouveau_montant = nouveau_produit['prix_unitaire'] * nouvelle_quantite

        stock_avant = nouveau_produit['quantite_stock']
        curseur.execute(
            "UPDATE produits SET quantite_stock = quantite_stock - %s WHERE id = %s",
            (nouvelle_quantite, nouveau_produit_id)
        )
        curseur.execute("""
            INSERT INTO mouvements_stock 
                (produit_id, pme_id, type_mouvement, quantite, stock_avant, stock_apres, motif, utilisateur_id)
            VALUES (%s, %s, 'correction', %s, %s, %s, %s, %s)
        """, (
            nouveau_produit_id, session['pme_id'], nouvelle_quantite,
            stock_avant, stock_avant - nouvelle_quantite,
            f"Correction vente #{vente_id} — nouvelle ligne", session['utilisateur_id']
        ))

        curseur.execute("""
            UPDATE ventes SET client_nom = %s, mode_paiement = %s, montant_total = %s
            WHERE id = %s AND pme_id = %s
        """, (client_nom, mode_paiement, nouveau_montant, vente_id, session['pme_id']))

        curseur.execute("""
            UPDATE lignes_vente SET produit_id = %s, quantite = %s, prix_unitaire = %s, sous_total = %s
            WHERE vente_id = %s
        """, (nouveau_produit_id, nouvelle_quantite, nouveau_produit['prix_unitaire'], nouveau_montant, vente_id))

        connexion.commit()
        connexion.close()

        return redirect('/ventes')

    except Exception as erreur:
        connexion.rollback()
        connexion.close()
        return f"Erreur lors de la modification : {erreur}", 500


@app.route('/ventes/annuler/<int:vente_id>')
@gerant_requis
def annuler_vente(vente_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    try:
        etat = get_etat_vente(curseur, vente_id, session['pme_id'])

        if etat is None:
            connexion.close()
            return "Vente introuvable", 404

        if etat == 'validee_dgi':
            connexion.close()
            return "Cette vente a été validée par la DGI et ne peut plus être annulée.", 403

        curseur.execute("SELECT * FROM lignes_vente WHERE vente_id = %s", (vente_id,))
        ligne = curseur.fetchone()

        curseur.execute("SELECT quantite_stock FROM produits WHERE id = %s", (ligne['produit_id'],))
        stock_avant = curseur.fetchone()['quantite_stock']
        stock_apres = stock_avant + ligne['quantite']

        curseur.execute(
            "UPDATE produits SET quantite_stock = %s WHERE id = %s",
            (stock_apres, ligne['produit_id'])
        )

        curseur.execute("""
            INSERT INTO mouvements_stock 
                (produit_id, pme_id, type_mouvement, quantite, stock_avant, stock_apres, motif, utilisateur_id)
            VALUES (%s, %s, 'correction', %s, %s, %s, %s, %s)
        """, (
            ligne['produit_id'], session['pme_id'], ligne['quantite'],
            stock_avant, stock_apres, f"Annulation vente #{vente_id}", session['utilisateur_id']
        ))

        curseur.execute("UPDATE ventes SET statut = 'annulee' WHERE id = %s", (vente_id,))

        # Si une facture existe pour cette vente, la marquer aussi comme annulée
        curseur.execute("UPDATE factures SET statut = 'annulee' WHERE vente_id = %s", (vente_id,))

        connexion.commit()
        connexion.close()

        return redirect('/ventes')

    except Exception as erreur:
        connexion.rollback()
        connexion.close()
        return f"Erreur lors de l'annulation : {erreur}", 500


@app.route('/ventes/supprimer/<int:vente_id>')
@gerant_requis
def supprimer_vente(vente_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    try:
        etat = get_etat_vente(curseur, vente_id, session['pme_id'])

        if etat is None:
            connexion.close()
            return "Vente introuvable", 404

        if etat != 'libre':
            connexion.close()
            return "Cette vente est déjà facturée. Utilisez l'annulation plutôt que la suppression.", 403

        # Vérifier si la vente avait déjà été annulée (stock déjà restauré dans ce cas)
        curseur.execute("SELECT statut FROM ventes WHERE id = %s", (vente_id,))
        vente_actuelle = curseur.fetchone()
        deja_annulee = vente_actuelle['statut'] == 'annulee'

        curseur.execute("SELECT * FROM lignes_vente WHERE vente_id = %s", (vente_id,))
        ligne = curseur.fetchone()

        if not deja_annulee:
            # Le stock n'a jamais été restauré pour cette vente : on le fait maintenant
            curseur.execute(
                "UPDATE produits SET quantite_stock = quantite_stock + %s WHERE id = %s",
                (ligne['quantite'], ligne['produit_id'])
            )

            curseur.execute("""
                INSERT INTO mouvements_stock 
                    (produit_id, pme_id, type_mouvement, quantite, stock_avant, stock_apres, motif, utilisateur_id)
                VALUES (%s, %s, 'correction', %s, 
                        (SELECT quantite_stock FROM produits WHERE id = %s) - %s,
                        (SELECT quantite_stock FROM produits WHERE id = %s),
                        %s, %s)
            """, (
                ligne['produit_id'], session['pme_id'], ligne['quantite'],
                ligne['produit_id'], ligne['quantite'],
                ligne['produit_id'],
                f"Suppression vente #{vente_id} (jamais facturée)", session['utilisateur_id']
            ))
        # Si déjà annulée, le stock a déjà été restauré lors de l'annulation — on ne touche plus au stock ici

        curseur.execute("DELETE FROM lignes_vente WHERE vente_id = %s", (vente_id,))
        curseur.execute("DELETE FROM ventes WHERE id = %s AND pme_id = %s", (vente_id, session['pme_id']))

        connexion.commit()
        connexion.close()


        return redirect('/ventes')

    except Exception as erreur:
        connexion.rollback()
        connexion.close()
        return f"Erreur lors de la suppression : {erreur}", 500


# ============================================
# MODULE STOCKS
# ============================================

@app.route('/stocks')
@gerant_requis
def liste_stocks():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT * FROM produits 
        WHERE pme_id = %s 
        ORDER BY nom ASC
    """, (session['pme_id'],))
    produits = curseur.fetchall()
    connexion.close()

    return render_template('liste_stocks.html', produits=produits)


@app.route('/stocks/nouveau')
@gerant_requis
def nouveau_produit():
    return render_template('nouveau_produit.html')


@app.route('/stocks/ajouter', methods=['POST'])
@gerant_requis
def ajouter_produit():
    nom = request.form['nom']
    categorie = request.form['categorie']
    prix_achat = request.form['prix_achat']
    prix_unitaire = request.form['prix_unitaire']
    quantite_stock = request.form['quantite_stock']
    seuil_alerte = request.form['seuil_alerte']

    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        INSERT INTO produits (pme_id, nom, categorie, prix_achat, prix_unitaire, quantite_stock, seuil_alerte)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (session['pme_id'], nom, categorie, prix_achat, prix_unitaire, quantite_stock, seuil_alerte))
    connexion.commit()
    connexion.close()

    return redirect('/stocks')


@app.route('/stocks/modifier/<int:produit_id>')
@gerant_requis
def modifier_produit(produit_id):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT * FROM produits WHERE id = %s AND pme_id = %s", (produit_id, session['pme_id']))
    produit = curseur.fetchone()
    connexion.close()

    if not produit:
        return "Produit introuvable", 404

    return render_template('modifier_produit.html', produit=produit)


@app.route('/stocks/mettre_a_jour/<int:produit_id>', methods=['POST'])
@gerant_requis
def mettre_a_jour_produit(produit_id):
    nom = request.form['nom']
    categorie = request.form['categorie']
    prix_achat = request.form['prix_achat']
    prix_unitaire = request.form['prix_unitaire']
    quantite_stock = request.form['quantite_stock']
    seuil_alerte = request.form['seuil_alerte']

    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        UPDATE produits 
        SET nom = %s, categorie = %s, prix_achat = %s, prix_unitaire = %s, quantite_stock = %s, seuil_alerte = %s
        WHERE id = %s AND pme_id = %s
    """, (nom, categorie, prix_achat, prix_unitaire, quantite_stock, seuil_alerte, produit_id, session['pme_id']))
    connexion.commit()
    connexion.close()

    return redirect('/stocks')


@app.route('/stocks/supprimer/<int:produit_id>')
@gerant_requis
def supprimer_produit(produit_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("SELECT * FROM produits WHERE id = %s AND pme_id = %s", (produit_id, session['pme_id']))
    produit = curseur.fetchone()

    if not produit:
        connexion.close()
        return "Produit introuvable", 404

    # Vérifier si ce produit a déjà été vendu au moins une fois
    curseur.execute("SELECT COUNT(*) AS nb FROM lignes_vente WHERE produit_id = %s", (produit_id,))
    deja_vendu = curseur.fetchone()['nb'] > 0

    if deja_vendu:
        # Désactivation seulement : l'historique des ventes dépend de ce produit
        curseur.execute("UPDATE produits SET actif = FALSE WHERE id = %s", (produit_id,))
        connexion.commit()
        connexion.close()
        return redirect('/stocks')

    # Jamais vendu : suppression définitive possible sans risque
    curseur.execute("DELETE FROM produits WHERE id = %s AND pme_id = %s", (produit_id, session['pme_id']))
    connexion.commit()
    connexion.close()

    return redirect('/stocks')


@app.route('/stocks/reactiver/<int:produit_id>')
@gerant_requis
def reactiver_produit(produit_id):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute(
        "UPDATE produits SET actif = TRUE WHERE id = %s AND pme_id = %s",
        (produit_id, session['pme_id'])
    )
    connexion.commit()
    connexion.close()

    return redirect('/stocks')


@app.route('/stocks/reapprovisionner/<int:produit_id>', methods=['POST'])
@gerant_requis
def reapprovisionner_produit(produit_id):
    quantite_ajoutee = int(request.form['quantite_ajoutee'])
    motif = request.form.get('motif', 'Réapprovisionnement')

    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("SELECT * FROM produits WHERE id = %s AND pme_id = %s", (produit_id, session['pme_id']))
    produit = curseur.fetchone()

    if not produit:
        connexion.close()
        return "Produit introuvable", 404

    stock_avant = produit['quantite_stock']
    stock_apres = stock_avant + quantite_ajoutee

    curseur.execute("UPDATE produits SET quantite_stock = %s WHERE id = %s", (stock_apres, produit_id))

    curseur.execute("""
        INSERT INTO mouvements_stock 
            (produit_id, pme_id, type_mouvement, quantite, stock_avant, stock_apres, motif, utilisateur_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        produit_id, session['pme_id'], 'entree_approvisionnement', quantite_ajoutee,
        stock_avant, stock_apres, motif, session['utilisateur_id']
    ))

    connexion.commit()
    connexion.close()

    return redirect('/stocks')


@app.route('/stocks/inventaire')
@gerant_requis
def inventaire_journalier():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT 
            m.id,
            m.type_mouvement,
            m.quantite,
            m.stock_avant,
            m.stock_apres,
            m.motif,
            m.date_mouvement,
            p.nom AS nom_produit,
            u.nom_complet AS nom_utilisateur
        FROM mouvements_stock m
        JOIN produits p ON p.id = m.produit_id
        LEFT JOIN utilisateurs u ON u.id = m.utilisateur_id
        WHERE m.pme_id = %s
        ORDER BY m.date_mouvement DESC
    """, (session['pme_id'],))
    mouvements = curseur.fetchall()
    connexion.close()

    return render_template('inventaire_journalier.html', mouvements=mouvements)


# ============================================
# MODULE FACTURES
# ============================================

def generer_numero_facture():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT COUNT(*) AS total FROM factures")
    resultat = curseur.fetchone()
    connexion.close()

    annee = 2026
    numero_sequence = resultat['total'] + 1
    return f"F-{annee}-{numero_sequence:03d}"


@app.route('/factures')
@gerant_requis
def liste_factures():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT 
            f.id,
            f.numero_facture,
            f.montant_ht,
            f.montant_tva,
            f.montant_ttc,
            f.date_emission,
            f.transmise_dgi,
            f.statut,
            v.client_nom
        FROM factures f
        JOIN ventes v ON v.id = f.vente_id
        WHERE v.pme_id = %s
        ORDER BY f.date_emission DESC
    """, (session['pme_id'],))
    factures = curseur.fetchall()
    connexion.close()

    return render_template('liste_factures.html', factures=factures)


@app.route('/factures/generer/<int:vente_id>')
@gerant_requis
def generer_facture(vente_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("SELECT * FROM ventes WHERE id = %s AND pme_id = %s", (vente_id, session['pme_id']))
    vente = curseur.fetchone()

    if not vente:
        connexion.close()
        return "Vente introuvable", 404

    curseur.execute("SELECT * FROM factures WHERE vente_id = %s", (vente_id,))
    facture_existante = curseur.fetchone()

    if facture_existante:
        connexion.close()
        return redirect(f"/factures/voir/{facture_existante['id']}")

    montant_ht = vente['montant_total']
    taux_tva = 18.00
    montant_tva = round(float(montant_ht) * taux_tva / 100, 2)
    montant_ttc = round(float(montant_ht) + montant_tva, 2)
    numero_facture = generer_numero_facture()

    curseur.execute("""
        INSERT INTO factures (vente_id, numero_facture, montant_ht, taux_tva, montant_tva, montant_ttc)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (vente_id, numero_facture, montant_ht, taux_tva, montant_tva, montant_ttc))
    connexion.commit()
    facture_id = curseur.lastrowid
    connexion.close()

    return redirect(f"/factures/voir/{facture_id}")


@app.route('/factures/voir/<int:facture_id>')
@gerant_requis
def voir_facture(facture_id):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT 
            f.*,
            v.client_nom,
            v.mode_paiement,
            p.nom AS nom_produit,
            lv.quantite,
            lv.prix_unitaire
        FROM factures f
        JOIN ventes v ON v.id = f.vente_id
        JOIN lignes_vente lv ON lv.vente_id = v.id
        JOIN produits p ON p.id = lv.produit_id
        WHERE f.id = %s AND v.pme_id = %s
    """, (facture_id, session['pme_id']))
    facture = curseur.fetchone()
    connexion.close()

    if not facture:
        return "Facture introuvable", 404

    return render_template('voir_facture.html', facture=facture)


# ============================================
# ESPACE DGI — SUPERVISION FISCALE (LECTURE SEULE)
# ============================================

@app.route('/dgi')
@dgi_requis
def dgi_dashboard():
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("""
        SELECT 
            p.id,
            p.nom_entreprise,
            p.ifu,
            COALESCE(SUM(f.montant_ht), 0) AS ca_total_ht,
            COALESCE(SUM(f.montant_tva), 0) AS tva_totale,
            COUNT(f.id) AS nb_factures
        FROM pme p
        LEFT JOIN ventes v ON v.pme_id = p.id
        LEFT JOIN factures f ON f.vente_id = v.id
        GROUP BY p.id, p.nom_entreprise, p.ifu
    """)
    pme_liste = curseur.fetchall()

    curseur.execute("""
        SELECT 
            COALESCE(SUM(montant_ht), 0) AS ca_global,
            COALESCE(SUM(montant_tva), 0) AS tva_globale,
            COUNT(*) AS total_factures
        FROM factures
    """)
    totaux = curseur.fetchone()

    connexion.close()

    return render_template('dgi_dashboard.html', pme_liste=pme_liste, totaux=totaux, nom=session['nom'])


@app.route('/dgi/pme/<int:pme_id>')
@dgi_requis
def dgi_detail_pme(pme_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("SELECT * FROM pme WHERE id = %s", (pme_id,))
    pme = curseur.fetchone()

    if not pme:
        connexion.close()
        return "PME introuvable", 404

    curseur.execute("""
        SELECT 
            f.numero_facture,
            f.montant_ht,
            f.montant_tva,
            f.montant_ttc,
            f.date_emission,
            f.transmise_dgi,
            v.client_nom
        FROM factures f
        JOIN ventes v ON v.id = f.vente_id
        WHERE v.pme_id = %s
        ORDER BY f.date_emission DESC
    """, (pme_id,))
    factures = curseur.fetchall()

    connexion.close()

    return render_template('dgi_detail_pme.html', pme=pme, factures=factures)


@app.route('/dgi/marquer_transmise/<int:facture_id>')
@dgi_requis
def marquer_transmise(facture_id):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("UPDATE factures SET transmise_dgi = TRUE WHERE id = %s", (facture_id,))
    connexion.commit()

    curseur.execute("""
        SELECT v.pme_id FROM factures f 
        JOIN ventes v ON v.id = f.vente_id 
        WHERE f.id = %s
    """, (facture_id,))
    resultat = curseur.fetchone()
    connexion.close()

    return redirect(f"/dgi/pme/{resultat['pme_id']}")


# ============================================
# MODULE BÉNÉFICES — Marge produit + Dépenses générales
# ============================================

@app.route('/depenses')
@gerant_requis
def liste_depenses():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT * FROM depenses
        WHERE pme_id = %s
        ORDER BY date_depense DESC
    """, (session['pme_id'],))
    depenses = curseur.fetchall()
    connexion.close()

    return render_template('liste_depenses.html', depenses=depenses)


@app.route('/depenses/ajouter', methods=['POST'])
@gerant_requis
def ajouter_depense():
    categorie = request.form['categorie']
    description = request.form['description']
    montant = request.form['montant']
    date_depense = request.form['date_depense']

    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        INSERT INTO depenses (pme_id, categorie, description, montant, date_depense, utilisateur_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (session['pme_id'], categorie, description, montant, date_depense, session['utilisateur_id']))
    connexion.commit()
    connexion.close()

    return redirect('/depenses')


@app.route('/benefices')
@gerant_requis
def benefices():
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("""
        SELECT 
            p.nom,
            SUM(lv.quantite) AS quantite_vendue,
            SUM(lv.quantite * p.prix_achat) AS cout_total,
            SUM(lv.sous_total) AS recette_totale,
            SUM(lv.sous_total - (lv.quantite * p.prix_achat)) AS marge_totale
        FROM lignes_vente lv
        JOIN produits p ON p.id = lv.produit_id
        JOIN ventes v ON v.id = lv.vente_id
        WHERE v.pme_id = %s
        GROUP BY p.id, p.nom
        ORDER BY marge_totale DESC
    """, (session['pme_id'],))
    marges_par_produit = curseur.fetchall()

    marge_brute_totale = sum(float(m['marge_totale']) for m in marges_par_produit)

    curseur.execute("""
        SELECT COALESCE(SUM(montant), 0) AS total_depenses
        FROM depenses
        WHERE pme_id = %s
    """, (session['pme_id'],))
    total_depenses = float(curseur.fetchone()['total_depenses'])

    connexion.close()

    benefice_net = marge_brute_totale - total_depenses

    return render_template(
        'benefices.html',
        marges_par_produit=marges_par_produit,
        marge_brute_totale=marge_brute_totale,
        total_depenses=total_depenses,
        benefice_net=benefice_net
    )


@app.route('/patrimoine')
@gerant_requis
def patrimoine():
    connexion = get_connexion()
    curseur = connexion.cursor()

    # Trésorerie = total des ventes encaissées (hors annulées) − total des dépenses
    curseur.execute("""
        SELECT COALESCE(SUM(montant_total), 0) AS total_ventes
        FROM ventes
        WHERE pme_id = %s AND statut != 'annulee'
    """, (session['pme_id'],))
    total_ventes = float(curseur.fetchone()['total_ventes'])

    curseur.execute("""
        SELECT COALESCE(SUM(montant), 0) AS total_depenses
        FROM depenses
        WHERE pme_id = %s
    """, (session['pme_id'],))
    total_depenses = float(curseur.fetchone()['total_depenses'])

    tresorerie = total_ventes - total_depenses

    # Valeur du stock actuel = quantité en stock × prix d'achat, pour les produits actifs
    curseur.execute("""
        SELECT 
            nom, quantite_stock, prix_achat,
            (quantite_stock * prix_achat) AS valeur_stock
        FROM produits
        WHERE pme_id = %s AND actif = TRUE
        ORDER BY valeur_stock DESC
    """, (session['pme_id'],))
    detail_stock = curseur.fetchall()

    valeur_stock_totale = sum(float(p['valeur_stock']) for p in detail_stock)

    connexion.close()

    patrimoine_total = tresorerie + valeur_stock_totale

    return render_template(
        'patrimoine.html',
        tresorerie=tresorerie,
        valeur_stock_totale=valeur_stock_totale,
        patrimoine_total=patrimoine_total,
        detail_stock=detail_stock
    )


@app.route('/recettes-journalieres')
@gerant_requis
def recettes_journalieres():
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("""
        SELECT 
            DATE(date_vente) AS jour,
            COALESCE(SUM(montant_total), 0) AS total_jour,
            COUNT(*) AS nombre_ventes
        FROM ventes
        WHERE pme_id = %s AND statut != 'annulee'
        GROUP BY DATE(date_vente)
        ORDER BY jour DESC
    """, (session['pme_id'],))
    recettes = curseur.fetchall()

    connexion.close()

    # Préparer les données pour le graphique (format simple, du plus ancien au plus récent)
    recettes_graphique = list(reversed(recettes))
    labels_jours = [r['jour'].strftime('%d/%m') for r in recettes_graphique]
    valeurs_jours = [float(r['total_jour']) for r in recettes_graphique]

    return render_template(
        'recettes_journalieres.html',
        recettes=recettes,
        labels_jours=labels_jours,
        valeurs_jours=valeurs_jours
    )


@app.route('/factures/pdf/<int:facture_id>')
@gerant_requis
def facture_pdf(facture_id):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT 
            f.*,
            v.client_nom,
            v.mode_paiement,
            p.nom AS nom_produit,
            lv.quantite,
            lv.prix_unitaire,
            pm.nom_entreprise,
            pm.adresse,
            pm.telephone,
            pm.ifu
        FROM factures f
        JOIN ventes v ON v.id = f.vente_id
        JOIN lignes_vente lv ON lv.vente_id = v.id
        JOIN produits p ON p.id = lv.produit_id
        JOIN pme pm ON pm.id = v.pme_id
        WHERE f.id = %s AND v.pme_id = %s
    """, (facture_id, session['pme_id']))
    facture = curseur.fetchone()
    connexion.close()

    if not facture:
        return "Facture introuvable", 404

    # ── Construction du PDF ──
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )

    styles = getSampleStyleSheet()
    vert = colors.HexColor('#1B4332')
    or_ = colors.HexColor('#C58F3C')
    gris = colors.HexColor('#5C5650')
    rouge = colors.HexColor('#A23B2E')

    style_titre = ParagraphStyle('titre', fontName='Helvetica-Bold', fontSize=22, textColor=vert, alignment=TA_LEFT)
    style_sous = ParagraphStyle('sous', fontName='Helvetica', fontSize=9, textColor=gris, alignment=TA_LEFT)
    style_num = ParagraphStyle('num', fontName='Helvetica-Bold', fontSize=14, textColor=vert, alignment=TA_RIGHT)
    style_label = ParagraphStyle('label', fontName='Helvetica', fontSize=9, textColor=gris)
    style_val = ParagraphStyle('val', fontName='Helvetica-Bold', fontSize=10, textColor=colors.black)
    style_total = ParagraphStyle('total', fontName='Helvetica-Bold', fontSize=14, textColor=vert, alignment=TA_RIGHT)
    style_note = ParagraphStyle('note', fontName='Helvetica', fontSize=8, textColor=gris)

    elements = []

    # ── En-tête : logo + numéro facture ──
    entete = Table([
        [
            [Paragraph("GestPME", style_titre),
             Paragraph("Plateforme sécurisée de gestion commerciale", style_sous),
             Paragraph(f"{facture['nom_entreprise']}", style_sous),
             Paragraph(f"{facture['adresse'] or ''}", style_sous),
             Paragraph(f"IFU : {facture['ifu']}", style_sous)],
            [Paragraph(f"{facture['numero_facture']}", style_num),
             Paragraph(f"Émise le {facture['date_emission'].strftime('%d/%m/%Y')}", style_sous)]
        ]
    ], colWidths=[11*cm, 6*cm])
    entete.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (1,0), (1,0), 'RIGHT'),
    ]))
    elements.append(entete)
    elements.append(HRFlowable(width="100%", thickness=2, color=vert, spaceAfter=14))

    # ── Informations client ──
    elements.append(Paragraph("Facturé à", style_label))
    elements.append(Paragraph(f"{facture['client_nom']}", style_val))
    elements.append(Paragraph(f"Mode de paiement : {facture['mode_paiement']}", style_sous))
    elements.append(Spacer(1, 0.6*cm))

    # ── Tableau des produits ──
    data_tableau = [
        ['Désignation', 'Quantité', 'Prix unitaire', 'Total HT']
    ]
    data_tableau.append([
        facture['nom_produit'],
        str(facture['quantite']),
        f"{float(facture['prix_unitaire']):,.0f} F".replace(',', ' '),
        f"{float(facture['montant_ht']):,.0f} F".replace(',', ' ')
    ])

    tableau = Table(data_tableau, colWidths=[8*cm, 2.5*cm, 3*cm, 3.5*cm])
    tableau.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), vert),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (1,0), (-1,-1), 'RIGHT'),
        ('ALIGN', (0,0), (0,-1), 'LEFT'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F0E9D8')]),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#DDD3BC')),
        ('TOPPADDING', (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    elements.append(tableau)
    elements.append(Spacer(1, 0.4*cm))

    # ── Totaux ──
    totaux = Table([
        ['Montant HT :', f"{float(facture['montant_ht']):,.0f} F".replace(',', ' ')],
        [f"TVA ({float(facture['taux_tva']):.0f}%) :", f"{float(facture['montant_tva']):,.0f} F".replace(',', ' ')],
        ['Total TTC :', f"{float(facture['montant_ttc']):,.0f} F".replace(',', ' ')],
    ], colWidths=[13*cm, 4*cm])
    totaux.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
        ('FONTNAME', (0,0), (-1,1), 'Helvetica'),
        ('FONTNAME', (0,2), (-1,2), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('TEXTCOLOR', (0,2), (-1,2), vert),
        ('FONTSIZE', (0,2), (-1,2), 13),
        ('TOPPADDING', (0,2), (-1,2), 6),
        ('LINEABOVE', (0,2), (-1,2), 1.5, vert),
    ]))
    elements.append(totaux)
    elements.append(Spacer(1, 0.8*cm))

    # ── Mention DGI ──
    elements.append(HRFlowable(width="100%", thickness=0.5, color=or_, spaceAfter=8))
    if facture['statut'] == 'annulee':
        mention = "✗ Cette facture a été annulée."
        couleur_mention = rouge
    elif facture['transmise_dgi']:
        mention = "✓ Cette facture a été transmise à la Direction Générale des Impôts (DGI) du Bénin pour supervision fiscale."
        couleur_mention = vert
    else:
        mention = "⏳ Cette facture sera transmise automatiquement à la DGI lors de la prochaine synchronisation."
        couleur_mention = gris

    style_mention = ParagraphStyle('mention', fontName='Helvetica', fontSize=8, textColor=couleur_mention)
    elements.append(Paragraph(mention, style_mention))

    # ── Pied de page ──
    elements.append(Spacer(1, 1*cm))
    elements.append(Paragraph(
        f"Document généré par GestPME · {facture['nom_entreprise']} · IFU : {facture['ifu']}",
        style_note
    ))

    doc.build(elements)
    buffer.seek(0)

    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=facture_{facture["numero_facture"]}.pdf'
    return response


@app.route('/mot-de-passe-oublie', methods=['GET', 'POST'])
def mot_de_passe_oublie():
    if request.method == 'POST':
        email = request.form['email']

        connexion = get_connexion()
        curseur = connexion.cursor()
        curseur.execute("SELECT id, nom_complet FROM utilisateurs WHERE email = %s", (email,))
        utilisateur = curseur.fetchone()

        if utilisateur:
            # Générer un token unique et sécurisé
            token = secrets.token_urlsafe(32)
            expire_at = datetime.now() + timedelta(minutes=15)

            # Invalider les anciens tokens de cet utilisateur
            curseur.execute("DELETE FROM reset_tokens WHERE utilisateur_id = %s", (utilisateur['id'],))

            # Sauvegarder le nouveau token
            curseur.execute("""
                INSERT INTO reset_tokens (utilisateur_id, token, expire_at)
                VALUES (%s, %s, %s)
            """, (utilisateur['id'], token, expire_at))
            connexion.commit()
            connexion.close()

            # Construire le lien de réinitialisation
            base_url = os.environ.get('APP_URL', 'http://127.0.0.1:5001')
            lien = f"{base_url}/reinitialiser/{token}"

            # Envoyer l'email
            msg = Message(
                subject="GestPME — Réinitialisation de votre mot de passe",
                recipients=[email]
            )
            msg.body = f"""Bonjour {utilisateur['nom_complet']},

Vous avez demandé une réinitialisation de votre mot de passe GestPME.

Cliquez sur ce lien pour choisir un nouveau mot de passe :
{lien}

Ce lien est valable pendant 15 minutes uniquement.

Si vous n'avez pas fait cette demande, ignorez cet email — votre mot de passe reste inchangé.

L'équipe GestPME
"""
            msg.html = f"""
<div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 24px;">
    <h2 style="color: #1B4332;">GestPME</h2>
    <p>Bonjour <strong>{utilisateur['nom_complet']}</strong>,</p>
    <p>Vous avez demandé une réinitialisation de votre mot de passe.</p>
    <p>
        <a href="{lien}" style="display:inline-block; padding:12px 24px; background:#1B4332; color:white; text-decoration:none; border-radius:6px; font-weight:bold;">
            Réinitialiser mon mot de passe
        </a>
    </p>
    <p style="color:#888; font-size:12px;">Ce lien est valable 15 minutes. Si vous n'avez pas fait cette demande, ignorez cet email.</p>
    <hr style="border:none; border-top:1px solid #DDD3BC;">
    <p style="color:#888; font-size:11px;">GestPME — Plateforme sécurisée de gestion commerciale pour PME béninoises</p>
</div>
"""
            mail.send(msg)

        else:
            connexion.close()

        # Toujours afficher le même message (sécurité : ne pas révéler si l'email existe)
        return render_template('mot_de_passe_oublie.html', message="Si cet email est enregistré, vous recevrez un lien de réinitialisation dans quelques instants.")

    return render_template('mot_de_passe_oublie.html', message=None)


@app.route('/reinitialiser/<token>', methods=['GET', 'POST'])
def reinitialiser_mot_de_passe(token):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT * FROM reset_tokens 
        WHERE token = %s AND utilise = FALSE AND expire_at > NOW()
    """, (token,))
    reset = curseur.fetchone()

    if not reset:
        connexion.close()
        return render_template('reinitialiser.html', erreur="Ce lien est invalide ou a expiré.", token=None)

    if request.method == 'POST':
        nouveau_mdp = request.form['nouveau_mot_de_passe']
        confirmation = request.form['confirmation']

        if nouveau_mdp != confirmation:
            connexion.close()
            return render_template('reinitialiser.html', erreur="Les mots de passe ne correspondent pas.", token=token)

        if len(nouveau_mdp) < 8:
            connexion.close()
            return render_template('reinitialiser.html', erreur="Le mot de passe doit contenir au moins 8 caractères.", token=token)

        # Mettre à jour le mot de passe
        nouveau_hash = generate_password_hash(nouveau_mdp)
        curseur.execute(
            "UPDATE utilisateurs SET mot_de_passe_hash = %s WHERE id = %s",
            (nouveau_hash, reset['utilisateur_id'])
        )

        # Invalider le token
        curseur.execute("UPDATE reset_tokens SET utilise = TRUE WHERE token = %s", (token,))
        connexion.commit()
        connexion.close()

        return redirect('/login')

    connexion.close()
    return render_template('reinitialiser.html', erreur=None, token=token)


# ============================================
# MODULE ADMIN PME — Supervision en temps réel
# ============================================

@app.route('/admin')
@admin_requis
def admin_dashboard():
    connexion = get_connexion()
    curseur = connexion.cursor()

    # Métriques globales de la PME
    curseur.execute("""
        SELECT 
            COALESCE(SUM(montant_total), 0) AS total_jour,
            COUNT(*) AS nombre_ventes
        FROM ventes
        WHERE pme_id = %s AND DATE(date_vente) = CURDATE() AND statut != 'annulee'
    """, (session['pme_id'],))
    ventes_jour = curseur.fetchone()

    curseur.execute("""
        SELECT 
            COALESCE(SUM(montant_total), 0) AS total_mois,
            COUNT(*) AS nombre_ventes_mois
        FROM ventes
        WHERE pme_id = %s AND MONTH(date_vente) = MONTH(CURDATE())
        AND YEAR(date_vente) = YEAR(CURDATE()) AND statut != 'annulee'
    """, (session['pme_id'],))
    ventes_mois = curseur.fetchone()

    # Dernières ventes (10 plus récentes)
    curseur.execute("""
        SELECT 
            v.id, v.client_nom, v.montant_total, v.mode_paiement,
            v.statut, v.date_vente, p.nom AS nom_produit,
            lv.quantite, u.nom_complet AS gerant_nom
        FROM ventes v
        JOIN lignes_vente lv ON lv.vente_id = v.id
        JOIN produits p ON p.id = lv.produit_id
        JOIN utilisateurs u ON u.id = v.utilisateur_id
        WHERE v.pme_id = %s
        ORDER BY v.date_vente DESC
        LIMIT 10
    """, (session['pme_id'],))
    dernieres_ventes = curseur.fetchall()

    # Notifications non lues
    curseur.execute("""
        SELECT * FROM notifications 
        WHERE pme_id = %s AND lue = FALSE
        ORDER BY date_creation DESC
    """, (session['pme_id'],))
    notifications = curseur.fetchall()

    # Liste des gérants de cette PME
    curseur.execute("""
        SELECT id, nom_complet, email, mfa_active
        FROM utilisateurs 
        WHERE pme_id = %s AND role = 'gerant'
    """, (session['pme_id'],))
    gerants = curseur.fetchall()

    connexion.close()

    return render_template(
        'admin_dashboard.html',
        nom=session['nom'],
        ventes_jour=ventes_jour,
        ventes_mois=ventes_mois,
        dernieres_ventes=dernieres_ventes,
        notifications=notifications,
        gerants=gerants
    )


@app.route('/admin/ventes')
@admin_requis
def admin_ventes():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("""
        SELECT 
            v.id, v.client_nom, v.montant_total, v.mode_paiement,
            v.statut, v.date_vente, p.nom AS nom_produit,
            lv.quantite, u.nom_complet AS gerant_nom,
            f.id AS facture_id, f.transmise_dgi
        FROM ventes v
        JOIN lignes_vente lv ON lv.vente_id = v.id
        JOIN produits p ON p.id = lv.produit_id
        JOIN utilisateurs u ON u.id = v.utilisateur_id
        LEFT JOIN factures f ON f.vente_id = v.id
        WHERE v.pme_id = %s
        ORDER BY v.date_vente DESC
    """, (session['pme_id'],))
    ventes = curseur.fetchall()
    connexion.close()

    return render_template('admin_ventes.html', ventes=ventes)


@app.route('/admin/ventes/rejeter/<int:vente_id>')
@admin_requis
def admin_rejeter_vente(vente_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    # Vérifier que la vente appartient bien à cette PME
    curseur.execute("SELECT * FROM ventes WHERE id = %s AND pme_id = %s", (vente_id, session['pme_id']))
    vente = curseur.fetchone()

    if not vente:
        connexion.close()
        return "Vente introuvable", 404

    # Restaurer le stock
    curseur.execute("SELECT * FROM lignes_vente WHERE vente_id = %s", (vente_id,))
    ligne = curseur.fetchone()

    curseur.execute(
        "UPDATE produits SET quantite_stock = quantite_stock + %s WHERE id = %s",
        (ligne['quantite'], ligne['produit_id'])
    )

    # Marquer comme annulée
    curseur.execute("UPDATE ventes SET statut = 'annulee' WHERE id = %s", (vente_id,))

    # Notification au gérant
    curseur.execute("""
        INSERT INTO notifications (pme_id, type_notification, message, vente_id)
        VALUES (%s, %s, %s, %s)
    """, (session['pme_id'], 'vente_rejetee', f"Vente #{vente_id:04d} rejetée par l'administrateur — stock restauré", vente_id))

    connexion.commit()
    connexion.close()

    return redirect('/admin/ventes')


@app.route('/admin/notifications/lire', methods=['POST'])
@admin_requis
def marquer_notifications_lues():
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("UPDATE notifications SET lue = TRUE WHERE pme_id = %s", (session['pme_id'],))
    connexion.commit()
    connexion.close()
    return redirect('/admin')


@app.route('/admin/rapport')
@admin_requis
def admin_rapport():
    connexion = get_connexion()
    curseur = connexion.cursor()

    # Rapport par gérant
    curseur.execute("""
        SELECT 
            u.nom_complet,
            COUNT(v.id) AS nb_ventes,
            COALESCE(SUM(v.montant_total), 0) AS total_ventes
        FROM utilisateurs u
        LEFT JOIN ventes v ON v.utilisateur_id = u.id AND v.statut != 'annulee'
        WHERE u.pme_id = %s AND u.role = 'gerant'
        GROUP BY u.id, u.nom_complet
        ORDER BY total_ventes DESC
    """, (session['pme_id'],))
    rapport_gerants = curseur.fetchall()

    # Rapport par produit
    curseur.execute("""
        SELECT 
            p.nom,
            SUM(lv.quantite) AS quantite_vendue,
            SUM(lv.sous_total) AS recette_totale
        FROM lignes_vente lv
        JOIN produits p ON p.id = lv.produit_id
        JOIN ventes v ON v.id = lv.vente_id
        WHERE v.pme_id = %s AND v.statut != 'annulee'
        GROUP BY p.id, p.nom
        ORDER BY recette_totale DESC
    """, (session['pme_id'],))
    rapport_produits = curseur.fetchall()

    connexion.close()

    return render_template(
        'admin_rapport.html',
        rapport_gerants=rapport_gerants,
        rapport_produits=rapport_produits
    )


# ── Événements WebSocket ──
@socketio.on('rejoindre_salle_admin')
def rejoindre_salle_admin(data):
    """L'admin rejoint sa salle privée pour recevoir les notifications de sa PME"""
    if 'pme_id' in session and session.get('role') == 'admin_pme':
        salle = f"admin_pme_{session['pme_id']}"
        join_room(salle)
        emit('connecte', {'message': f'Connecté à la salle {salle}'})


# ============================================
# MODULE SUPER-ADMIN — Tableau de bord GestPME
# ============================================

@app.route('/super-admin')
@super_admin_requis
def super_admin_dashboard():
    connexion = get_connexion()
    curseur = connexion.cursor()

    # Statistiques globales
    curseur.execute("SELECT COUNT(*) AS total FROM pme")
    total_pme = curseur.fetchone()['total']

    curseur.execute("SELECT COUNT(*) AS total FROM utilisateurs WHERE role = 'gerant'")
    total_gerants = curseur.fetchone()['total']

    curseur.execute("SELECT COALESCE(SUM(montant_total), 0) AS total FROM ventes WHERE statut != 'annulee'")
    ca_global = curseur.fetchone()['total']

    curseur.execute("SELECT COUNT(*) AS total FROM ventes WHERE statut != 'annulee'")
    total_ventes = curseur.fetchone()['total']

    curseur.execute("SELECT COUNT(*) AS total FROM factures WHERE statut = 'active'")
    total_factures = curseur.fetchone()['total']

    # Liste détaillée des PME avec leurs stats
    curseur.execute("""
        SELECT 
            p.id,
            p.nom_entreprise,
            p.ifu,
            p.telephone,
            p.adresse,
            p.date_creation,
            COUNT(DISTINCT u.id) AS nb_utilisateurs,
            COUNT(DISTINCT v.id) AS nb_ventes,
            COALESCE(SUM(v.montant_total), 0) AS ca_total
        FROM pme p
        LEFT JOIN utilisateurs u ON u.pme_id = p.id AND u.role = 'gerant'
        LEFT JOIN ventes v ON v.pme_id = p.id AND v.statut != 'annulee'
        GROUP BY p.id, p.nom_entreprise, p.ifu, p.telephone, p.adresse, p.date_creation
        ORDER BY ca_total DESC
    """)
    pme_liste = curseur.fetchall()

    # PME les plus actives (top 5)
    curseur.execute("""
        SELECT 
            p.nom_entreprise,
            COUNT(v.id) AS nb_ventes,
            COALESCE(SUM(v.montant_total), 0) AS ca_total
        FROM pme p
        LEFT JOIN ventes v ON v.pme_id = p.id AND v.statut != 'annulee'
        GROUP BY p.id, p.nom_entreprise
        ORDER BY ca_total DESC
        LIMIT 5
    """)
    top_pme = curseur.fetchall()

    # Inscriptions par jour (7 derniers jours)
    curseur.execute("""
        SELECT DATE(date_creation) AS jour, COUNT(*) AS nb
        FROM pme
        GROUP BY DATE(date_creation)
        ORDER BY jour DESC
        LIMIT 7
    """)
    inscriptions = curseur.fetchall()

    connexion.close()

    labels = [str(i['jour']) for i in reversed(inscriptions)]
    valeurs = [i['nb'] for i in reversed(inscriptions)]

    return render_template(
        'super_admin_dashboard.html',
        nom=session['nom'],
        total_pme=total_pme,
        total_gerants=total_gerants,
        ca_global=ca_global,
        total_ventes=total_ventes,
        total_factures=total_factures,
        pme_liste=pme_liste,
        top_pme=top_pme,
        labels=labels,
        valeurs=valeurs
    )


@app.route('/super-admin/pme/<int:pme_id>')
@super_admin_requis
def super_admin_detail_pme(pme_id):
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("SELECT * FROM pme WHERE id = %s", (pme_id,))
    pme = curseur.fetchone()

    if not pme:
        connexion.close()
        return "PME introuvable", 404

    # Utilisateurs de cette PME
    curseur.execute("""
        SELECT id, nom_complet, email, role, mfa_active, date_creation
        FROM utilisateurs WHERE pme_id = %s
    """, (pme_id,))
    utilisateurs = curseur.fetchall()

    # Dernières ventes
    curseur.execute("""
        SELECT v.*, p.nom AS nom_produit, lv.quantite
        FROM ventes v
        JOIN lignes_vente lv ON lv.vente_id = v.id
        JOIN produits p ON p.id = lv.produit_id
        WHERE v.pme_id = %s
        ORDER BY v.date_vente DESC
        LIMIT 10
    """, (pme_id,))
    ventes = curseur.fetchall()

    # Statistiques
    curseur.execute("""
        SELECT 
            COALESCE(SUM(montant_total), 0) AS ca_total,
            COUNT(*) AS nb_ventes
        FROM ventes WHERE pme_id = %s AND statut != 'annulee'
    """, (pme_id,))
    stats = curseur.fetchone()

    connexion.close()

    return render_template(
        'super_admin_detail_pme.html',
        pme=pme,
        utilisateurs=utilisateurs,
        ventes=ventes,
        stats=stats
    )


# ============================================
# MODULE ABONNEMENTS — FedaPay Mobile Money
# ============================================

@app.route('/abonnement')
@gerant_requis
def abonnement():
    connexion = get_connexion()
    curseur = connexion.cursor()

    # Vérifier l'abonnement actuel de la PME
    curseur.execute("""
        SELECT * FROM abonnements
        WHERE pme_id = %s AND statut = 'actif'
        AND date_fin > NOW()
        ORDER BY date_fin DESC LIMIT 1
    """, (session['pme_id'],))
    abonnement_actuel = curseur.fetchone()
    connexion.close()

    return render_template(
        'abonnement.html',
        plans=PLANS,
        abonnement_actuel=abonnement_actuel,
        fedapay_public_key=FEDAPAY_PUBLIC_KEY
    )


@app.route('/abonnement/payer/<plan>', methods=['GET', 'POST'])
@gerant_requis
def payer_abonnement(plan):
    if plan not in PLANS:
        return "Plan invalide", 400

    plan_info = PLANS[plan]

    if plan_info['prix'] == 0:
        # Plan gratuit — activer directement
        connexion = get_connexion()
        curseur = connexion.cursor()
        date_debut = datetime.now()
        date_fin = date_debut + timedelta(days=365)
        curseur.execute("""
            INSERT INTO abonnements (pme_id, plan, montant, statut, date_debut, date_fin)
            VALUES (%s, %s, %s, 'actif', %s, %s)
        """, (session['pme_id'], plan, 0, date_debut, date_fin))
        connexion.commit()
        connexion.close()
        return redirect('/dashboard')

    if request.method == 'POST':
        telephone = request.form['telephone']
        operateur = request.form['operateur']

        try:
            # Créer la transaction FedaPay
            transaction = Transaction.create({
                'description': f'Abonnement GestPME — Plan {plan_info["nom"]}',
                'amount': int(plan_info['prix']),
                'currency': {'iso': 'XOF'},
                'callback_url': f"{os.environ.get('APP_URL', 'http://127.0.0.1:5001')}/abonnement/confirmer",
                'customer': {
                    'email': session.get('email', 'client@gestpme.bj'),
                },
            })

            # Sauvegarder en attente
            connexion = get_connexion()
            curseur = connexion.cursor()
            curseur.execute("""
                INSERT INTO abonnements (pme_id, plan, montant, statut, transaction_id)
                VALUES (%s, %s, %s, 'en_attente', %s)
            """, (session['pme_id'], plan, plan_info['prix'], str(transaction.id)))
            connexion.commit()
            connexion.close()

            # Rediriger vers la page de paiement FedaPay
            return redirect(transaction.links.payment_url)

        except Exception as e:
            return render_template('payer_abonnement.html',
                                   plan=plan,
                                   plan_info=plan_info,
                                   erreur=f"Erreur : {str(e)}")

    return render_template('payer_abonnement.html', plan=plan, plan_info=plan_info, erreur=None)


@app.route('/abonnement/confirmer')
def confirmer_abonnement():
    transaction_id = request.args.get('id')
    statut = request.args.get('status')

    if statut == 'approved' and transaction_id:
        connexion = get_connexion()
        curseur = connexion.cursor()

        curseur.execute("SELECT * FROM abonnements WHERE transaction_id = %s", (transaction_id,))
        abonnement_row = curseur.fetchone()

        if abonnement_row:
            date_debut = datetime.now()
            date_fin = date_debut + timedelta(days=30)

            curseur.execute("""
                UPDATE abonnements
                SET statut = 'actif', date_debut = %s, date_fin = %s
                WHERE transaction_id = %s
            """, (date_debut, date_fin, transaction_id))
            connexion.commit()
            connexion.close()

            return redirect('/dashboard')

    return redirect('/abonnement')


if __name__ == '__main__':
    socketio.run(app, debug=True, host='127.0.0.1', port=5001)
