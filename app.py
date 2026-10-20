import os
import io
import base64
from datetime import datetime
from dateutil.relativedelta import relativedelta
import streamlit as st
import pandas as pd
import psycopg
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

# --- STRICT BUTTON-ONLY CLICK, CUSTOM UPLOAD TEXT & SIZE TAG CSS ---
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
        conn = psycopg.connect(db_url, autocommit=True)
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
    ("f_val", 0), ("f_gsps", ""), ("f_reg", ""), 
    ("f_pdf", ""), ("fetch_id_input", ""), 
    ("last_fetched_id", ""), 
    ("upload_reset_counter", 0), ("trigger_reset", False),
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
        st.session_state.logged_in = False
        st.error("⚠️ Your account has been deactivated by an administrator. Session terminated.")
        st.stop()

# ==========================================
# AUTHENTICATION / LOGIN SCREEN
# ==========================================
if not st.session_state.logged_in:
    st.markdown("<h2 style='text-align: center; color: #1f538d;'>GSPS MHA Record Management System</h2>", unsafe_allow_html=True)
    st.markdown("<h4 style='text-align: center; color: gray;'>Please log in to continue</h4>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        with st.form("login_form"):
            uname = st.text_input("Username").strip().upper()
            pwd = st.text_input("Password", type="password")
            submit = st.form_submit_button("Login", use_container_width=True)
            
            if submit:
                cursor.execute("SELECT name, role, username, p_entry, p_reports, p_trash, p_users, status FROM users WHERE username = %s AND password = %s", (uname, pwd.strip()))
                user = cursor.fetchone()
                if user:
                    if user[7] != 'ACTIVE':
                        st.error("⚠️ Your account is inactive. Please contact the administrator.")
                    else:
                        st.session_state.logged_in = True
                        st.session_state.name = user[0]
                        st.session_state.role = user[1]
                        st.session_state.username = user[2]
                        
                        st.session_state.p_entry = user[3]
                        st.session_state.p_reports = user[4]
                        st.session_state.p_trash = user[5]
                        st.session_state.p_users = 1 if user[1] == 'Administrator' else user[6]
                        st.toast(f"Welcome back, {user[0]}!", icon="👋")
                        st.rerun()
                else:
                    st.error("Invalid username or password!")
    st.stop()

# ==========================================
# MAIN DASHBOARD (AFTER LOGIN)
# ==========================================
title_banner = f"""
    <div style="background-color: #1f538d; padding: 12px 20px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; color: white;">
        <h3 style="margin: 0;">GSPS MHA Record Management System</h3>
        <span style="font-size: 16px; font-weight: bold; color: #ffeb3b;">👤 {st.session_state.role}: {st.session_state.name}</span>
    </div>
"""
st.markdown(title_banner, unsafe_allow_html=True)
st.write("")

with st.sidebar:
    st.write(f"Welcome, **{st.session_state.name}**")
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.logged_in = False
        st.rerun()

# --- HELPER FUNCTIONS ---
def validate_date(date_str):
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

def validate_gsps(gsps_val):
    if not gsps_val:
        return False
    gsps_list = [g.strip() for g in gsps_val.replace("\n", ",").split(",") if g.strip()]
    if not gsps_list:
        return False
    for g in gsps_list:
        if not g.isdigit() or len(g) != 10:
            return False
    return True

def check_duplicate(noc_num, noc_date_val, gsps_val, region_val, exclude_id=None):
    clean_noc = noc_num.strip().upper()
    clean_date = noc_date_val.strip()
    clean_gsps = gsps_val.strip()
    clean_region = region_val.strip().upper()
    
    cursor.execute("SELECT record_id, mha_noc_number, mha_noc_date, gsps_number, allowed_region FROM n_records WHERE status = 'ACTIVE'")
    for row in cursor.fetchall():
        r_id, db_noc, db_date, db_gsps, db_region = row
        if exclude_id and r_id == exclude_id:
            continue
        if (db_noc.strip().upper() == clean_noc and 
            db_date.strip() == clean_date and 
            db_gsps.strip() == clean_gsps and 
            db_region.strip().upper() == clean_region):
            return True
    return False

def get_existing_noc_copy(noc_num, noc_date, org_name):
    if not noc_num.strip() or not noc_date.strip() or not org_name.strip():
        return ""
    cursor.execute(
        "SELECT mha_noc_copy FROM n_records WHERE UPPER(mha_noc_number) = %s AND mha_noc_date = %s AND UPPER(organisation) = %s AND mha_noc_copy != '' AND mha_noc_copy IS NOT NULL LIMIT 1", 
        (noc_num.strip().upper(), noc_date.strip(), org_name.strip().upper())
    )
    res = cursor.fetchone()
    return res[0] if res else ""

def upload_file_to_supabase(file_obj, filename):
    """Uploads file buffer to Supabase Storage bucket and returns public URL."""
    try:
        file_bytes = file_obj.getvalue()
        supabase_client.storage.from_(BUCKET_NAME).upload(
            path=filename,
            file=file_bytes,
            file_options={"content-type": file_obj.type, "upsert": "true"}
        )
        public_url = supabase_client.storage.from_(BUCKET_NAME).get_public_url(filename)
        return public_url
    except Exception as e:
        try:
            supabase_client.storage.from_(BUCKET_NAME).update(
                path=filename,
                file=file_obj.getvalue(),
                file_options={"content-type": file_obj.type, "upsert": "true"}
            )
            return supabase_client.storage.from_(BUCKET_NAME).get_public_url(filename)
        except Exception as err:
            st.error(f"Storage Upload Error: {err}")
            return ""

def delete_file_from_supabase(file_url):
    """Deletes a file from Supabase storage using its public URL."""
    if not file_url:
        return
    try:
        # Extract filename from public URL
        parts = file_url.split(f"/{BUCKET_NAME}/")
        if len(parts) > 1:
            filename = parts[1].split("?")[0] # remove query params if any
            supabase_client.storage.from_(BUCKET_NAME).remove([filename])
    except Exception as e:
        print(f"Warning: Could not delete old file from storage: {e}")

def is_expired(noc_date_str, validity_val):
    if not validity_val or not str(validity_val).isdigit():
        return False
    try:
        noc_dt = datetime.strptime(noc_date_str, "%d.%m.%Y")
        expiry_dt = noc_dt + relativedelta(years=int(validity_val))
        return datetime.now() > expiry_dt
    except ValueError:
        return False

def reset_form_state():
    st.session_state["trigger_reset"] = True

def on_fetch_id_change():
    """Callback triggered when user enters a Record ID next to the Update button."""
    fetch_val = st.session_state.get("fetch_id_input", "").strip()
    if not fetch_val:
        st.session_state["last_fetched_id"] = ""
        return
    if fetch_val.isdigit():
        target_id = int(fetch_val)
        cursor.execute(
            "SELECT mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, mha_noc_copy, status FROM n_records WHERE record_id = %s",
            (target_id,)
        )
        fetched_data = cursor.fetchone()
        if not fetched_data:
            st.toast(f"⚠️ Record ID #{target_id} does not exist.", icon="⚠️")
        elif fetched_data[7] == 'DELETED':
            st.toast(f"⚠️ Record ID #{target_id} is already deleted.", icon="⚠️")
        else:
            st.session_state["f_noc"] = fetched_data[0]
            st.session_state["f_date"] = fetched_data[1]
            st.session_state["f_org"] = fetched_data[2]
            st.session_state["f_val"] = int(fetched_data[3]) if fetched_data[3] and str(fetched_data[3]).isdigit() else 0
            st.session_state["f_gsps"] = fetched_data[4]
            st.session_state["f_reg"] = fetched_data[5]
            st.session_state["f_pdf"] = fetched_data[6] if fetched_data[6] else ""
            st.session_state["last_fetched_id"] = fetch_val
            st.toast(f"Record ID #{target_id} loaded! Modify fields above and click 'Update Record'.", icon="✅")
    else:
        st.toast("⚠️ Please enter a valid numeric Record ID.", icon="⚠️")


# ==========================================
# TAB BUILDER FUNCTIONS
# ==========================================
def render_entry_tab():
    if st.session_state.get("trigger_reset", False):
        st.session_state["f_noc"] = ""
        st.session_state["f_date"] = ""
        st.session_state["f_org"] = ""
        st.session_state["f_val"] = 0
        st.session_state["f_gsps"] = ""
        st.session_state["f_reg"] = ""
        st.session_state["f_pdf"] = ""
        st.session_state["fetch_id_input"] = ""
        st.session_state["last_fetched_id"] = ""
        st.session_state["upload_reset_counter"] += 1
        st.session_state["trigger_reset"] = False

    st.subheader("Record Entry & Management")

    with st.container():
        st.markdown("**MHA NOC Information**")
        col1, col2 = st.columns(2)
        with col1:
            noc_num = st.text_input("NOC Number*", key="f_noc", placeholder="e.g., 11016/...")
            org = st.text_input("Organisation*", key="f_org", placeholder="e.g., BSNL / Inmarsat")
        with col2:
            noc_date = st.text_input("NOC Date (DD.MM.YYYY)*", key="f_date", placeholder="DD.MM.YYYY")
            validity = st.number_input("Validity (Years)", min_value=0, step=1, format="%d", key="f_val")

        st.markdown("**GSPS Numbers and Permitted Restricted Areas**")
        gsps_num = st.text_area("GSPS Number(s) (Comma/Newline sep)*", key="f_gsps", height=65, placeholder="Enter 10-digit GSPS numbers...")

        r_col1, r_col2 = st.columns([1, 1])
        with r_col1:
            region = st.text_input("Allowed Region*", key="f_reg", placeholder="Pan India / North")
        with r_col2:
            uploaded_file = st.file_uploader(
                "MHA NOC Upload", 
                type=["pdf", "jpg", "jpeg"], 
                key=f"pdf_uploader_{st.session_state.upload_reset_counter}"
            )

        file_error = False
        if uploaded_file is not None:
            file_ext = os.path.splitext(uploaded_file.name)[1].lower()
            if file_ext not in [".pdf", ".jpg", ".jpeg"]:
                file_error = True
                st.error("⚠️ Error: Only PDF and JPG/JPEG files are allowed.")
            elif uploaded_file.size > 5 * 1024 * 1024:
                file_error = True
                st.error("⚠️ Error: File size exceeds the 5 MB limit. Please attach a smaller file.")
            else:
                st.markdown(f'<span style="color: #1f538d; font-weight: bold; font-size: 12px;">📎 Uploaded: {uploaded_file.name}</span>', unsafe_allow_html=True)

        shared_noc_copy = ""
        if noc_num.strip() and noc_date.strip() and org.strip():
            shared_noc_copy = get_existing_noc_copy(noc_num.strip(), noc_date.strip(), org.strip())

        active_file_path = st.session_state.get("f_pdf", "") or shared_noc_copy

        if uploaded_file is None and active_file_path:
            st.markdown(f'<span style="color: #1f538d; font-weight: bold; font-size: 12px;">📎 Linked NOC Document Attached in Cloud</span>', unsafe_allow_html=True)
            st.markdown(f"[🔗 Open / Download Linked Document]({active_file_path})")

        st.markdown("")
        b1, b_fetch, b2, b3 = st.columns([1.2, 1.1, 1.2, 1])
        with b1:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            save_btn = st.button("💾 Save New Record(s)", use_container_width=True)
        with b_fetch:
            fetch_id = st.text_input(
                "🔍 Fetch ID (to Update)",
                placeholder="Type ID & press Enter",
                key="fetch_id_input",
                on_change=on_fetch_id_change
            )
        with b2:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            update_btn = st.button("✏️ Update Record", use_container_width=True)
        with b3:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            clear_btn = st.button("🔄 Clear / Reset", use_container_width=True)

        if clear_btn:
            st.toast("Form cleared successfully!", icon="🔄")
            reset_form_state()
            st.rerun()

        if save_btn:
            clean_noc_num = noc_num.strip().upper()
            clean_org = org.strip().upper()
            clean_region = region.strip().upper()

            if fetch_id.strip():
                st.error("⚠️ Please clear the 'Fetch ID' box or click Clear / Reset before saving a new entry.")
            elif file_error:
                st.error("⚠️ Cannot save: Please fix the file attachment errors above.")
            elif not clean_noc_num or not noc_date or not clean_org or not gsps_num or not clean_region:
                st.error("All mandatory fields must be filled out before saving or uploading a file!")
            elif not validate_date(noc_date):
                st.error("Invalid date format! Use DD.MM.YYYY.")
            elif datetime.strptime(noc_date, "%d.%m.%Y").date() > datetime.now().date():
                st.error("⚠️ NOC Date cannot be greater than the present date.")
            elif not validate_gsps(gsps_num):
                st.error("Each GSPS number must be exactly 10 digits.")
            else:
                gsps_list = [g.strip() for g in gsps_num.replace("\n", ",").split(",") if g.strip()]
                if len(gsps_list) != len(set(gsps_list)):
                    st.error("Duplicate GSPS numbers found in bulk list.")
                else:
                    conflict = False
                    for g in gsps_list:
                        if check_duplicate(clean_noc_num, noc_date, g, clean_region):
                            st.error(f"Duplicate entry exists for NOC '{clean_noc_num}', Date '{noc_date}', GSPS '{g}', Region '{clean_region}'.")
                            conflict = True
                            break
                    if not conflict:
                        old_noc_copy = get_existing_noc_copy(clean_noc_num, noc_date, clean_org)
                        final_pdf_path = shared_noc_copy
                        if uploaded_file is not None and not file_error:
                            safe_noc = clean_noc_num.replace("/", "_").replace("\\", "_")
                            safe_date = noc_date.replace(".", "_")
                            safe_org = clean_org.replace("/", "_").replace("\\", "_").replace(" ", "_")
                            ext = os.path.splitext(uploaded_file.name)[1].lower()
                            filename = f"NOC_{safe_noc}_{safe_date}_{safe_org}{ext}"
                            
                            final_pdf_path = upload_file_to_supabase(uploaded_file, filename)
                            st.session_state["f_pdf"] = final_pdf_path

                        for g in gsps_list:
                            cursor.execute(
                                "INSERT INTO n_records (mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, mha_noc_copy, status, created_by, modified_by) VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s)",
                                (clean_noc_num, noc_date, clean_org, str(validity), g, clean_region, final_pdf_path, st.session_state.name, st.session_state.name)
                            )
                        
                        if final_pdf_path:
                            cursor.execute(
                                "UPDATE n_records SET mha_noc_copy = %s WHERE UPPER(mha_noc_number) = %s AND mha_noc_date = %s AND UPPER(organisation) = %s AND status = 'ACTIVE'",
                                (final_pdf_path, clean_noc_num, noc_date, clean_org)
                            )
                        
                        st.toast(f"Successfully saved {len(gsps_list)} record(s) for NOC '{clean_noc_num}'!", icon="✅")
                        reset_form_state()
                        st.rerun()

        if update_btn:
            clean_noc_num = noc_num.strip().upper()
            clean_org = org.strip().upper()
            clean_region = region.strip().upper()

            parsed_gsps_list = [g.strip() for g in gsps_num.replace("\n", ",").split(",") if g.strip()]

            if not fetch_id.strip() or not fetch_id.strip().isdigit():
                st.error("⚠️ To update a record, first enter its numeric ID in 'Fetch ID (to Update)' right next to this button and press Enter.")
            elif st.session_state.get("last_fetched_id") != fetch_id.strip():
                on_fetch_id_change()
                st.rerun()
            elif file_error:
                st.error("⚠️ Cannot update: Please fix the file attachment errors above.")
            elif not clean_noc_num or not noc_date or not clean_org or not gsps_num or not clean_region:
                st.error("All mandatory fields must be filled out before updating!")
            elif len(parsed_gsps_list) > 1:
                st.error("Bulk entries with multiple numbers are only allowed when saving new records. Please provide a single GSPS number for updates.")
            elif not validate_date(noc_date):
                st.error("Invalid date format! Use DD.MM.YYYY.")
            elif datetime.strptime(noc_date, "%d.%m.%Y").date() > datetime.now().date():
                st.error("⚠️ NOC Date cannot be greater than the present date.")
            elif not validate_gsps(gsps_num):
                st.error("GSPS Number must be exactly 10 digits.")
            else:
                target_id = int(fetch_id.strip())
                single_gsps = parsed_gsps_list[0]
                if check_duplicate(clean_noc_num, noc_date, single_gsps, clean_region, exclude_id=target_id):
                    st.error("Another active record with these details (NOC, Date, GSPS, Region) already exists!")
                else:
                    # Fetch old attachment copy for this specific combination before updating
                    old_noc_copy = get_existing_noc_copy(clean_noc_num, noc_date, clean_org)
                    final_pdf_path = shared_noc_copy
                    
                    if uploaded_file is not None and not file_error:
                        # Delete old file from Supabase storage if it exists
                        if old_noc_copy:
                            delete_file_from_supabase(old_noc_copy)

                        safe_noc = clean_noc_num.replace("/", "_").replace("\\", "_")
                        safe_date = noc_date.replace(".", "_")
                        safe_org = clean_org.replace("/", "_").replace("\\", "_").replace(" ", "_")
                        ext = os.path.splitext(uploaded_file.name)[1].lower()
                        filename = f"NOC_{safe_noc}_{safe_date}_{safe_org}{ext}"
                        
                        # Upload new file to Supabase Storage
                        final_pdf_path = upload_file_to_supabase(uploaded_file, filename)
                        st.session_state["f_pdf"] = final_pdf_path

                    # 1. Update ONLY the explicitly fetched row's values (e.g., allowed region, GSPS number, validity, etc.)
                    cursor.execute(
                        "UPDATE n_records SET mha_noc_number = %s, mha_noc_date = %s, organisation = %s, validity = %s, gsps_number = %s, allowed_region = %s, modified_by = %s WHERE record_id = %s",
                        (clean_noc_num, noc_date, clean_org, str(validity), single_gsps, clean_region, st.session_state.name, target_id)
                    )
                    
                    # 2. If a new attachment file was uploaded, update/propagate ONLY the attachment across all active rows having the same NOC Number + Date + Organisation combination
                    if uploaded_file is not None and not file_error and final_pdf_path:
                        cursor.execute(
                            "UPDATE n_records SET mha_noc_copy = %s WHERE UPPER(mha_noc_number) = %s AND mha_noc_date = %s AND UPPER(organisation) = %s AND status = 'ACTIVE'",
                            (final_pdf_path, clean_noc_num, noc_date, clean_org)
                        )
                    
                    st.toast(f"Record ID #{target_id} updated successfully!", icon="✅")
                    reset_form_state()
                    st.rerun()

    st.markdown("---")
    st.subheader("Active Records View")
    
    sc1, sc2, sc3 = st.columns([3, 2, 2])
    with sc1: search_query = st.text_input("🔍 Search records...", placeholder="Type to search...", key="search_active")
    with sc2: filter_from = st.text_input("From Date (DD.MM.YYYY)", placeholder="Optional", key="filter_from_active")
    with sc3: filter_to = st.text_input("To Date (DD.MM.YYYY)", placeholder="Optional", key="filter_to_active")

    cursor.execute("SELECT record_id, mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, mha_noc_copy, created_by, modified_by FROM n_records WHERE status = 'ACTIVE'")
    all_rows = cursor.fetchall()

    filtered_data = []
    for r in all_rows:
        r_id, noc, dt, org_name, val, gsps, reg, noc_copy, creator, modifier = r
        if search_query and not any(search_query.lower() in str(val).lower() for val in r):
            continue
        if filter_from and validate_date(filter_from):
            if datetime.strptime(dt, "%d.%m.%Y") < datetime.strptime(filter_from, "%d.%m.%Y"):
                continue
        if filter_to and validate_date(filter_to):
            if datetime.strptime(dt, "%d.%m.%Y") > datetime.strptime(filter_to, "%d.%m.%Y"):
                continue
        
        expired_flag = "⚠️ Expired" if is_expired(dt, val) else "Active"
        pdf_display = "📄 Attached" if noc_copy else "No File"
        filtered_data.append({
            "Select": False,
            "Record ID": r_id,
            "NOC Number": noc,
            "NOC Date": dt,
            "Organisation": org_name,
            "Validity (Yrs)": val,
            "GSPS Number": gsps,
            "Allowed Region": reg,
            "PDF Copy": pdf_display,
            "Created By": creator,
            "Modified By": modifier,
            "Status": expired_flag
        })

    if filtered_data:
        df_active = pd.DataFrame(filtered_data)
        edited_df = st.data_editor(df_active, hide_index=True, use_container_width=True)
        
        selected_to_delete = edited_df[edited_df["Select"] == True]["Record ID"].tolist()
        if selected_to_delete:
            if st.button("🗑️ Delete Selected Records", type="primary", key="del_active_btn"):
                for r_id in selected_to_delete:
                    cursor.execute("UPDATE n_records SET status = 'DELETED', modified_by = %s WHERE record_id = %s", (st.session_state.name, r_id))
                st.toast(f"Successfully moved {len(selected_to_delete)} record(s) to Deleted Records.", icon="🗑️")
                st.rerun()
    else:
        st.info("No active records found matching criteria.")


def render_reports_tab():
    st.subheader("Reports & Analytics (Grouped by MHA NOC)")
    
    rc1, rc2, rc3, rc4 = st.columns(4)
    with rc1: rep_start = st.text_input("Start Date (DD.MM.YYYY)", placeholder="Optional", key="r_start")
    with rc2: rep_end = st.text_input("End Date (DD.MM.YYYY)", placeholder="Optional", key="r_end")
    with rc3: rep_reg = st.text_input("Allowed Region Filter", placeholder="Optional", key="r_reg")
    with rc4: rep_noc = st.text_input("NOC Number Filter", placeholder="Optional", key="r_noc")

    cursor.execute("SELECT mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, mha_noc_copy FROM n_records WHERE status = 'ACTIVE' ORDER BY mha_noc_number, mha_noc_date")
    report_rows = cursor.fetchall()

    filtered_report_rows = []
    for r in report_rows:
        noc, dt, org, val, gsps, reg, noc_copy = r
        if rep_reg and rep_reg.lower() not in reg.lower(): continue
        if rep_noc and rep_noc.lower() not in noc.lower(): continue
        if rep_start and validate_date(rep_start):
            if datetime.strptime(dt, "%d.%m.%Y") < datetime.strptime(rep_start, "%d.%m.%Y"): continue
        if rep_end and validate_date(rep_end):
            if datetime
