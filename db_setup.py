import sqlite3


def setup_db():
    conn = sqlite3.connect('quantum_pay.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS transactions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 gateway TEXT,
                 fee REAL,
                 latency REAL,
                 timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()


