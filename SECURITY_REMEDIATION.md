# Gest PME 2.0 — Plan de remédiation sécurité

Branche : `security/gestpme-2.0`  
Base : `main`  
Mode : préparation en branche isolée ; aucun déploiement Render.

## Règles de sécurité

- Ne jamais publier de valeur de secret dans le code, les journaux, les rapports ou les commits.
- Révoquer toute clé/mot de passe qui a déjà été commité : supprimer une valeur du dernier commit ne la retire pas de l'historique.
- Ne jamais considérer une variable d'environnement absente comme une autorisation d'utiliser un secret de secours.
- Ne pas activer une intégration DGI ou un paiement sans validation réelle côté serveur et accord du fournisseur.
- Toute opération métier doit être liée au `pme_id` de la session et vérifiée côté serveur.

## P0 — Bloquant avant paiements/données réelles

### 1. Secrets et configuration
Constat dans `app.py` : plusieurs paramètres sensibles utilisent des valeurs de repli dans le code (clé Flask, identifiants e-mail, code d'inscription admin, secret bootstrap, secret de sauvegarde).  
Remédiation :
- Lire les secrets exclusivement depuis l'environnement.
- Refuser le démarrage en production si `SECRET_KEY` ou un secret requis manque ou conserve une valeur de développement.
- Faire tourner les identifiants concernés et vérifier l'historique Git.
- Conserver uniquement des exemples factices dans un éventuel fichier `.env.example`.

### 2. Confirmation Kkiapay
Constat : `/abonnement/kkiapay/confirmer` crée un abonnement actif à partir d'un `transaction_id` et d'un `plan` reçus du client ; la fonction examinée ne vérifie pas la transaction auprès de Kkiapay.  
Remédiation :
- Vérifier la transaction côté serveur avec l'API officielle Kkiapay.
- Vérifier statut payé, référence, montant, devise et correspondance au plan.
- Garantir l'idempotence (une transaction ne peut activer qu'un seul abonnement).
- Ne jamais accepter le prix envoyé par le navigateur comme source de vérité.
- Ajouter des tests : transaction inconnue, non payée, montant incorrect, doublon, mauvais compte PME.

### 3. Mutations exposées en GET
Constat : plusieurs routes GET modifient ou suppriment des données : annulation/suppression de ventes, suppression/réactivation de produits, génération de facture et marquage DGI.  
Remédiation :
- Passer ces routes en POST (ou DELETE pour une API).
- Remplacer les liens HTML par des formulaires POST avec confirmation utilisateur.
- Ajouter CSRF avant de réactiver ces actions.
- Préserver les contrôles de propriété `pme_id` et les règles métier.

## P1 — Protection des comptes et des données

### 4. CSRF
Constat : aucune protection CSRF globale n'a été identifiée dans le code et les formulaires examinés.  
Remédiation : intégrer Flask-WTF/CSRFProtect, ajouter le jeton à chaque formulaire POST et transmettre le jeton dans les requêtes AJAX. Tester tous les formulaires avant mise en production.

### 5. Limitation des tentatives
Constat : aucune limitation visible sur les routes de connexion et de vérification MFA dans les extraits examinés.  
Remédiation : appliquer une limitation par compte et par adresse IP, délais progressifs, journalisation sans données sensibles et protection contre l'énumération de comptes.

### 6. Sessions et cookies
Remédiation :
- En production : `SESSION_COOKIE_SECURE=True`, `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE='Lax'`.
- Définir une durée de session raisonnable et renouveler la session après authentification.
- Effacer toute session pré-authentification après succès/échec MFA selon le parcours.
- Vérifier la configuration effective derrière le proxy Render.

### 7. WebSocket / CORS
Constat : Socket.IO utilise `cors_allowed_origins='*'`.  
Remédiation : autoriser uniquement les domaines officiels de production et de préproduction ; authentifier les événements sensibles et vérifier l'appartenance à la PME côté serveur à chaque événement.

### 8. Sauvegardes
Constat : le déclenchement de sauvegarde utilise un secret dans l'URL.  
Remédiation : remplacer par un mécanisme signé/rotatif ou un endpoint privé avec authentification forte, éviter le secret dans les journaux/proxies et surveiller les exécutions. Tester la restauration, pas seulement la création de l'archive.

## P2 — Fiabilité et conformité

### 9. Vérification IFU / DGI
Constat : si l'API n'est pas configurée ou est indisponible, la fonction peut accepter un IFU sur la seule base de son format.  
Remédiation : afficher explicitement « format vérifié uniquement — statut DGI non vérifié » ; ne jamais présenter cela comme une validation DGI. N'activer l'appel réel qu'après confirmation de l'API et autorisation officielle.

### 10. TLS base de données
Constat : la configuration examinée active SSL sans validation stricte du certificat CA.  
Remédiation : utiliser le certificat CA fourni par l'hébergeur DB et activer la vérification du certificat/nom d'hôte conformément au pilote utilisé.

### 11. Dépendances
Constat : `requirements.txt` liste les dépendances sans versions fixées.  
Remédiation : produire un verrouillage reproductible depuis l'environnement de déploiement, vérifier les avis de sécurité et mettre à jour de façon contrôlée. Ne pas choisir des versions arbitraires sans tests de compatibilité.

## Critères d'acceptation avant fusion

- Tests automatisés verts sur Python et la base de test.
- Aucun secret réel ou valeur de secours dans le dépôt.
- Tous les POST sensibles rejetés sans jeton CSRF valide.
- Toutes les mutations refusent GET.
- Paiement vérifié auprès de Kkiapay et idempotent.
- Tests d'isolation multi-PME réussis.
- Cookies de session sécurisés en production.
- CORS limité aux domaines attendus.
- Sauvegarde restaurée avec succès dans un environnement de test.
- Aucun déploiement ni changement de configuration Render sans approbation explicite.
