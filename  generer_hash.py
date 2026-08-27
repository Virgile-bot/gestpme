# generer_hash.py
from werkzeug.security import generate_password_hash
hash = generate_password_hash("admin2026")
print(hash)