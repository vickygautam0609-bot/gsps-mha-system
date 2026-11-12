import os
import io
import base64
from datetime import datetime
from dateutil.relativedelta import relativedelta
import streamlit as st
import pandas as pd
import psycopg2
import plotly.express as px
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from supabase import create_client, Client

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="GSPS MHA Record Management System",
    page_icon="📡",
    layout="wide"
)

# --- STRICT BUTTON-ONLY CLICK, CUSTOM UPLOAD TEXT & TABLE HEADER CSS ---
st.markdown(
    """
    <style>
    /* Disable clicks on the entire drag-and-drop dropzone box/row */
    [data-testid="stFileUploader"] section {
        pointer-events: none !important;
        background-color: transparent !important;
        border: none !important;
        padding: 0px !important;
        min-height: unset !important;
    }
    /* Enable clicks strictly on the inner browse/upload button */
    [data-testid="stFileUploader"] section button {
        pointer-events: auto !important;
        background-color: #1f538d !important;
        color: white !important;
        border-radius: 4px !important;
        padding: 0.35rem 0.75rem !important;
        border: none !important;
        font-size: 0px !important; /* Hide original text/icon */
    }
    [data-testid="stFileUploader"] section button:hover {
        background-color: #163d6b !important;
        color: white !important;
    }
    /* Inject custom text "UPLOAD" onto the button */
    [data-testid="stFileUploader"] section button::after {
        content: "📁 UPLOAD" !important;
        font-size: 13px !important;
        font-weight: bold !important;
        color: white !important;
    }
    /* Hide markdown container text */
    [data-testid="stFileUploader"] [data-testid="stMarkdownContainer"] {
        display: none !important;
    }
    /* Completely hide default size limit message */
    [data-testid="stFileUploader"] small {
        display: none !important;
    }
    /* Style form/action buttons blue */
    div.stButton > button {
        background-color: #1f538d !important;
        color: white !important;
        border: none !important;
    }
    div.stButton > button:hover {
        background-color: #163d6b !important;
        color: white !important;
    }
    /* Color data table / editor headers blue */
    [data-testid="stDataEditor"] [role="columnheader"] {
        background-color: #1f538d !important;
        color: white !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# --- SUPABASE STORAGE & DATABASE SETUP ---
@st.cache_resource
def get_supabase_client():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase_client = get_supabase_client()
BUCKET_NAME = "noc_documents"

@st.cache_resource
def get_db_connection():
    db_url = st.secrets["SUPABASE_DB_URL"]
    if "sslmode" not in db_url:
        separator = "&" if "?" in db_url else "?"
        db_url = f"{db_url}{separator}sslmode=require"
        
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        return conn
    except Exception as e:
        st.error("🚨 **Database Connection Error!**")
        st.code(f"Technical Error: {e}", language="text")
        st.stop()

conn = get_db_connection()
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS n_records (
        record_id SERIAL PRIMARY KEY,
        mha_noc_number TEXT,
        mha_noc_date TEXT,
        organisation TEXT,
        validity TEXT,
        gsps_number TEXT,
        allowed_region TEXT,
        mha_noc_copy TEXT,
        status TEXT DEFAULT 'ACTIVE',
        created_by TEXT DEFAULT 'System',
        modified_by TEXT DEFAULT 'System'
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id SERIAL PRIMARY KEY,
        name TEXT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT,
        p_entry INTEGER DEFAULT 1,
        p_reports INTEGER DEFAULT 1,
        p_trash INTEGER DEFAULT 1,
        p_users INTEGER DEFAULT 0,
        status TEXT DEFAULT 'ACTIVE'
    )
""")

# Check and update schema for existing user tables if columns are missing
cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
user_cols = [col[0] for col in cursor.fetchall()]
if "p_entry" not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS p_entry INTEGER DEFAULT 1")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS p_reports INTEGER DEFAULT 1")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS p_trash INTEGER DEFAULT 1")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS p_users INTEGER DEFAULT 0")
if "status" not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ACTIVE'")

# Insert default admin user if table is empty
cursor.execute("SELECT COUNT(*) FROM users WHERE username = 'ADMIN'")
if cursor.fetchone()[0] == 0:
    cursor.execute(
        "INSERT INTO users (name, username, password, role, p_entry, p_reports, p_trash, p_users, status) VALUES (%s, %s, %s, %s, 1, 1, 1, 1, 'ACTIVE')",
        ("Administrator", "ADMIN", "admin123", "Administrator"),
    )
else:
    cursor.execute(
        "UPDATE users SET p_users = 1, role = 'Administrator', status = 'ACTIVE' WHERE username = 'ADMIN'"
    )

# --- SESSION STATE INITIALIZATION ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.name = ""
    st.session_state.role = ""
    st.session_state.username = ""
    st.session_state.p_entry = 1
    st.session_state.p_reports = 1
    st.session_state.p_trash = 1
    st.session_state.p_users = 0

for key, default_val in [
    ("f_noc", ""), ("f_date", ""), ("f_org", ""), 
    ("f_val_y", 0), ("f_val_m", 0), ("f_val_d", 0), 
    ("f_gsps", ""), ("f_reg", ""), 
    ("f_pdf", ""), ("fetch_id_input", ""), 
    ("last_fetched_id", ""), 
    ("upload_reset_counter", 0), ("trigger_reset", False),
    ("load_noc", ""), ("load_date", ""), ("load_org", ""),
    ("load_val_y", 0), ("load_val_m", 0), ("load_val_d", 0),
    ("load_gsps", ""), ("load_reg", ""), ("load_pdf", ""),
    ("new_user_name", ""), ("new_user_uname", ""), ("new_user_pwd", ""),
    ("new_user_role", "Basic User"), ("new_p_entry", True), 
    ("new_p_reports", True), ("new_p_trash", True), ("new_p_users", False),
    ("new_user_counter", 0)
]:
    if key not in st.session_state:
        st.session_state[key] = default_val

# --- SECURITY CHECK: DROP ACTIVE SESSIONS IF MARKED INACTIVE ---
if st.session_state.logged_in:
    cursor.execute("SELECT status FROM users WHERE username = %s", (st.session_state.username,))
    u_status_row = cursor.fetchone()
    if not u_status_row or u_status_row[0] != 'ACTIVE':
