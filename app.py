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

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="GSPS MHA Record Management System",
    page_icon="📡",
    layout="wide"
)

# --- STRICT BUTTON-ONLY CLICK & REMOVE SIZE LIMIT TEXT CSS ---
st.markdown(
    """
    <style>
    /* Disable pointer events on the entire file uploader dropzone container */
    [data-testid="stFileUploader"] {
        pointer-events: none !important;
    }
    /* Re-enable pointer events strictly on the upload button */
    [data-testid="stFileUploader"] button {
        pointer-events: auto !important;
        background-color: #1f538d !important;
        color: white !important;
        border-radius: 4px !important;
        padding: 0.35rem 0.75rem !important;
        border: none !important;
        font-size: 0px !important; /* Hide original text/icon */
    }
    [data-testid="stFileUploader"] button:hover {
        background-color: #163d6b !important;
        color: white !important;
    }
    /* Inject custom text "UPLOAD" and upload icon onto the button */
    [data-testid="stFileUploader"] button::after {
        content: "📁 UPLOAD" !important;
        font-size: 13px !important;
        font-weight: bold !important;
        color: white !important;
    }
    /* Completely hide all instructions, drag-drop text, and size limit labels */
    [data-testid="stFileUploader"] [data-testid="stMarkdownContainer"],
    [data-testid="stFileUploader"] small,
    [data-testid="stFileUploader"] div[data-testid="stText"],
    [data-testid="stFileUploader"] span {
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

# --- DIRECTORY SETUP FOR COPIES ---
UPLOAD_DIR = "uploaded_nocs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- DATABASE SETUP (SUPABASE POSTGRESQL - SAFE DEBUGGER) ---
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
        st.markdown("Please check your **Streamlit Secrets (`SUPABASE_DB_URL`)**. Common fixes:")
        st.markdown("1. Ensure you removed any square brackets `[]` around your password.")
        st.markdown("2. Ensure you are using the **Session Pooler** URI from Supabase (Port `6543`).")
        st.markdown("3. Check if your password contains special characters that need URL encoding.")
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
        p_users INTEGER DEFAULT 0
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

# Insert default admin user if table is empty, and ensure admin always has User Management privilege locked on
cursor.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
if cursor.fetchone()[0] == 0:
    cursor.execute(
        "INSERT INTO users (name, username, password, role, p_entry, p_reports, p_trash, p_users) VALUES (%s, %s, %s, %s, 1, 1, 1, 1)",
        ("Administrator", "admin", "admin123", "Administrator"),
    )
else:
    cursor.execute(
        "UPDATE users SET p_users = 1, role = 'Administrator' WHERE username = 'admin'"
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

# Form fields session state initialization
if "f_noc" not in st.session_state: st.session_state.f_noc = ""
if "f_date" not in st.session_state: st.session_state.f_date = ""
if "f_org" not in st.session_state: st.session_state.f_org = ""
if "f_val" not in st.session_state: st.session_state.f_val = 0
if "f_gsps" not in st.session_state: st.session_state.f_gsps = ""
if "f_reg" not in st.session_state: st.session_state.f_reg = ""
if "f_pdf" not in st.session_state: st.session_state.f_pdf = ""
if "upload_reset_counter" not in st.session_state: st.session_state.upload_reset_counter = 0
if "last_fetched_id" not in st.session_state: st.session_state.last_fetched_id = ""
if "priv_success_msg" not in st.session_state: st.session_state.priv_success_msg = ""
if "entry_success_msg" not in st.session_state: st.session_state.entry_success_msg = ""

# ==========================================
# AUTHENTICATION / LOGIN SCREEN
# ==========================================
if not st.session_state.logged_in:
    st.markdown("<h2 style='text-align: center; color: #1f538d;'>GSPS MHA Record Management System</h2>", unsafe_allow_html=True)
    st.markdown("<h4 style='text-align: center; color: gray;'>Please log in to continue</h4>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        with st.form("login_form"):
            uname = st.text_input("Username")
            pwd = st.text_input("Password", type="password")
            submit = st.form_submit_button("Login", use_container_width=True)
            
            if submit:
                cursor.execute("SELECT name, role, username, p_entry, p_reports, p_trash, p_users FROM users WHERE username = %s AND password = %s", (uname.strip(), pwd.strip()))
                user = cursor.fetchone()
                if user:
                    st.session_state.logged_in = True
                    st.session_state.name = user[0]
                    st.session_state.role = user[1]
                    st.session_state.username = user[2]
                    
                    st.session_state.p_entry = user[3]
                    st.session_state.p_reports = user[4]
                    st.session_state.p_trash = user[5]
                    st.session_state.p_users = 1 if user[1] == 'Administrator' else user[6]
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
    clean_noc = noc_num.strip().lower()
    clean_date = noc_date_val.strip()
    clean_gsps = gsps_val.strip()
    clean_region = region_val.strip().lower()
    
    cursor.execute("SELECT record_id, mha_noc_number, mha_noc_date, gsps_number, allowed_region FROM n_records WHERE status = 'ACTIVE'")
    for row in cursor.fetchall():
        r_id, db_noc, db_date, db_gsps, db_region = row
        if exclude_id and r_id == exclude_id:
            continue
        if (db_noc.strip().lower() == clean_noc and 
            db_date.strip() == clean_date and 
            db_gsps.strip() == clean_gsps and 
            db_region.strip().lower() == clean_region):
            return True
    return False

def get_existing_noc_copy(noc_num, noc_date):
    cursor.execute("SELECT mha_noc_copy FROM n_records WHERE LOWER(mha_noc_number) = %s AND mha_noc_date = %s AND mha_noc_copy != '' AND mha_noc_copy IS NOT NULL LIMIT 1", (noc_num.strip().lower(), noc_date.strip()))
    res = cursor.fetchone()
    return res[0] if res else ""

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
    for k in ["f_noc", "f_date", "f_org", "f_val", "f_gsps", "f_reg", "f_pdf", "fetch_id_input"]:
        if k in st.session_state:
            del st.session_state[k]
    st.session_state.f_val = 0
    st.session_state.last_fetched_id = ""
    st.session_state.upload_reset_counter += 1


# ==========================================
# TAB BUILDER FUNCTIONS
# ==========================================
def render_entry_tab():
    st.subheader("Record Entry & Management")
    
    if st.session_state.entry_success_msg:
        st.success(st.session_state.entry_success_msg)
        st.session_state.entry_success_msg = ""

    # Fetch Record ID Bar
    col_fetch, col_clear_space = st.columns([2, 6])
    with col_fetch:
        fetch_id = st.text_input("Fetch by Record ID (for Updates)", placeholder="Enter ID...", key="fetch_id_input")
    
    if fetch_id.strip() and fetch_id.strip() != st.session_state.last_fetched_id:
        if fetch_id.strip().isdigit():
            target_id = int(fetch_id.strip())
            cursor.execute("SELECT mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, mha_noc_copy, status FROM n_records WHERE record_id = %s", (target_id,))
            fetched_data = cursor.fetchone()
            if not fetched_data:
                st.warning(f"⚠️ Record ID #{target_id} does not exist.")
            elif fetched_data[7] == 'DELETED':
                st.warning(f"⚠️ Record ID #{target_id} is already deleted.")
            else:
                st.session_state.f_noc = fetched_data[0]
                st.session_state.f_date = fetched_data[1]
                st.session_state.f_org = fetched_data[2]
                st.session_state.f_val = int(fetched_data[3]) if fetched_data[3] and str(fetched_data[3]).isdigit() else 0
                st.session_state.f_gsps = fetched_data[4]
                st.session_state.f_reg = fetched_data[5]
                st.session_state.f_pdf = fetched_data[6] if fetched_data[6] else ""
                st.session_state.last_fetched_id = fetch_id.strip()
                st.success(f"✅ Record ID #{target_id} loaded successfully for updating.")
        else:
            st.error("Please enter a valid numeric Record ID.")

    st.markdown("---")

    # --- COMPACT STRUCTURED FORM CONTAINER ---
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

        # Parallel Row for Allowed Region, Attachment, and Action Buttons
        r_col1, r_col2, r_col3, r_col4, r_col5 = st.columns([2, 2, 1.2, 1.2, 1.2])
        with r_col1:
            region = st.text_input("Allowed Region*", key="f_reg", placeholder="Pan India / North")
        with r_col2:
            uploaded_file = st.file_uploader(
                "Browse", 
                type=["pdf", "jpg", "jpeg"], 
                key=f"pdf_uploader_{st.session_state.upload_reset_counter}"
            )
        with r_col3:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            save_btn = st.button("💾 Save New Record(s)", use_container_width=True)
        with r_col4:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            update_btn = st.button("✏️ Update Record", use_container_width=True)
        with r_col5:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            clear_btn = st.button("🔄 Clear / Reset", use_container_width=True)

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
                if uploaded_file.type == "application/pdf":
                    st.download_button(
                        "📥 Open / Download Uploaded PDF",
                        data=uploaded_file.getvalue(),
                        file_name=uploaded_file.name,
                        mime="application/pdf",
                        key="preview_uploaded_pdf"
                    )
                else:
                    with st.expander("👁️ Preview Image"):
                        st.image(uploaded_file, caption=uploaded_file.name, use_container_width=True)

        shared_noc_copy = ""
        if noc_num.strip() and noc_date.strip():
            shared_noc_copy = get_existing_noc_copy(noc_num.strip(), noc_date.strip())

        active_file_path = st.session_state.get("f_pdf", "") or shared_noc_copy

        if uploaded_file is None and active_file_path and os.path.exists(active_file_path):
            file_name_lower = active_file_path.lower()
            st.markdown(f'<span style="color: #1f538d; font-weight: bold; font-size: 12px;">📎 Attached: {os.path.basename(active_file_path)}</span>', unsafe_allow_html=True)
            with open(active_file_path, "rb") as fp:
                file_bytes = fp.read()
            if file_name_lower.endswith(".pdf"):
                st.download_button(
                    "📥 Open / Download Attached PDF",
                    data=file_bytes,
                    file_name=os.path.basename(active_file_path),
                    mime="application/pdf",
                    key="preview_attached_pdf"
                )
            else:
                with st.expander("👁️ Preview Attached Document"):
                    st.image(file_bytes, caption=os.path.basename(active_file_path), use_container_width=True)

        if clear_btn:
            reset_form_state()
            st.success("Form cleared successfully!")
            st.rerun()

        if save_btn:
            if fetch_id.strip():
                st.error("⚠️ Please clear the 'Fetch by Record ID' field or click Clear before saving a new entry.")
            elif file_error:
                st.error("⚠️ Cannot save: Please fix the file attachment errors above.")
            elif not noc_num or not noc_date or not org or not gsps_num or not region:
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
                        if check_duplicate(noc_num, noc_date, g, region):
                            st.error(f"Duplicate entry exists for NOC '{noc_num}', Date '{noc_date}', GSPS '{g}', Region '{region}'.")
                            conflict = True
                            break
                    if not conflict:
                        old_noc_copy = get_existing_noc_copy(noc_num, noc_date)
                        final_pdf_path = shared_noc_copy
                        if uploaded_file is not None and not file_error:
                            safe_noc = noc_num.replace("/", "_").replace("\\", "_")
                            safe_date = noc_date.replace(".", "_")
                            ext = os.path.splitext(uploaded_file.name)[1].lower()
                            filename = f"NOC_{safe_noc}_{safe_date}{ext}"
                            final_pdf_path = os.path.join(UPLOAD_DIR, filename)
                            
                            if old_noc_copy and os.path.exists(old_noc_copy) and os.path.abspath(old_noc_copy) != os.path.abspath(final_pdf_path):
                                try:
                                    os.remove(old_noc_copy)
                                except Exception:
                                    pass

                            with open(final_pdf_path, "wb") as f:
                                f.write(uploaded_file.getbuffer())
                            st.session_state.f_pdf = final_pdf_path

                        for g in gsps_list:
                            cursor.execute(
                                "INSERT INTO n_records (mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, mha_noc_copy, status, created_by, modified_by) VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s)",
                                (noc_num, noc_date, org, str(validity), g, region, final_pdf_path, st.session_state.name, st.session_state.name)
                            )
                        
                        st.session_state.entry_success_msg = f"✅ Confirmation: Successfully saved {len(gsps_list)} new record(s) for NOC '{noc_num}'!"
                        reset_form_state()
                        st.rerun()

        if update_btn:
            if not fetch_id.strip() or not fetch_id.isdigit():
                st.error("Please enter a valid numeric Record ID in 'Fetch by Record ID' above to update.")
            elif file_error:
                st.error("⚠️ Cannot update: Please fix the file attachment errors above.")
            elif not noc_num or not noc_date or not org or not gsps_num or not region:
                st.error("All mandatory fields must be filled out before updating!")
            elif "," in gsps_num or "\n" in gsps_num:
                st.error("Bulk entries with multiple numbers are only allowed when saving new records.")
            elif not validate_date(noc_date):
                st.error("Invalid date format! Use DD.MM.YYYY.")
            elif datetime.strptime(noc_date, "%d.%m.%Y").date() > datetime.now().date():
                st.error("⚠️ NOC Date cannot be greater than the present date.")
            elif not validate_gsps(gsps_num):
                st.error("GSPS Number must be exactly 10 digits.")
            else:
                target_id = int(fetch_id.strip())
                if check_duplicate(noc_num, noc_date, gsps_num, region, exclude_id=target_id):
                    st.error("Another active record with these details (NOC, Date, GSPS, Region) already exists!")
                else:
                    old_noc_copy = get_existing_noc_copy(noc_num, noc_date)
                    final_pdf_path = shared_noc_copy
                    if uploaded_file is not None and not file_error:
                        safe_noc = noc_num.replace("/", "_").replace("\\", "_")
                        safe_date = noc_date.replace(".", "_")
                        ext = os.path.splitext(uploaded_file.name)[1].lower()
                        filename = f"NOC_{safe_noc}_{safe_date}{ext}"
                        final_pdf_path = os.path.join(UPLOAD_DIR, filename)
                        
                        if old_noc_copy and os.path.exists(old_noc_copy) and os.path.abspath(old_noc_copy) != os.path.abspath(final_pdf_path):
                            try:
                                os.remove(old_noc_copy)
                            except Exception:
                                pass

                        with open(final_pdf_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                        st.session_state.f_pdf = final_pdf_path

                    cursor.execute(
                        "UPDATE n_records SET mha_noc_number = %s, mha_noc_date = %s, organisation = %s, validity = %s, gsps_number = %s, allowed_region = %s, mha_noc_copy = %s, modified_by = %s WHERE record_id = %s",
                        (noc_num, noc_date, org, str(validity), gsps_num, region, final_pdf_path, st.session_state.name, target_id)
                    )
                    
                    st.session_state.entry_success_msg = f"✅ Confirmation: Record ID #{target_id} updated successfully!"
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
        pdf_display = "📄 Attached" if (noc_copy and os.path.exists(noc_copy)) else "No File"
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
                st.success(f"Successfully moved {len(selected_to_delete)} record(s) to Deleted Records.")
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
            if datetime.strptime(dt, "%d.%m.%Y") > datetime.strptime(rep_end, "%d.%m.%Y"): continue
        filtered_report_rows.append(r)

    if filtered_report_rows:
        grouped_data = {}
        for r in filtered_report_rows:
            noc, dt, org, val, gsps, reg, noc_copy = r
            key = (noc, dt, org, val, noc_copy)
            if key not in grouped_data:
                grouped_data[key] = []
            grouped_data[key].append((gsps, reg))

        st.caption(f"ℹ️ *Total Unique MHA NOCs: {len(grouped_data)} | Total GSPS Entries: {len(filtered_report_rows)}*")
        st.markdown("---")

        s_no_counter = 1
        display_grouped_rows = []

        for i, (noc_key, items) in enumerate(grouped_data.items()):
            noc, dt, org, val, noc_copy = noc_key
            
            st.markdown(
                f"""
                <div style="background-color: #eaf2f8; padding: 10px 15px; border-left: 5px solid #1f538d; border-radius: 4px; margin-bottom: 5px;">
                    <strong>MHA NOC Number:</strong> <span style="color: #1f538d;">{noc}</span> &nbsp;&nbsp;|&nbsp;&nbsp; 
                    <strong>Date:</strong> {dt} &nbsp;&nbsp;|&nbsp;&nbsp; 
                    <strong>Organisation:</strong> {org} &nbsp;&nbsp;|&nbsp;&nbsp; 
                    <strong>Validity:</strong> {val if val else 'N/A'} Yrs
                </div>
                """,
                unsafe_allow_html=True
            )
            
            if noc_copy and os.path.exists(noc_copy):
                with st.expander("👁️ Preview / Download Attached File"):
                    file_name_lower = noc_copy.lower()
                    with open(noc_copy, "rb") as fp:
                        file_bytes = fp.read()
                    
                    if file_name_lower.endswith(".pdf"):
                        base64_pdf = base64.b64encode(file_bytes).decode('utf-8')
                        pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="450px" type="application/pdf"></iframe>'
                        st.markdown(pdf_display, unsafe_allow_html=True)
                    else:
                        st.image(file_bytes, caption=os.path.basename(noc_copy), use_container_width=True)
                    
                    st.write("")
                    st.download_button(
                        "📥 Download File",
                        data=file_bytes,
                        file_name=os.path.basename(noc_copy),
                        mime="application/octet-stream",
                        key=f"dl_pdf_{i}_{noc}_{dt}"
                    )
            else:
                st.caption("*(No file attached for this NOC)*")

            sub_table_data = []
            for item_idx, (gsps, reg) in enumerate(items, start=1):
                sub_table_data.append({
                    "S.No.": s_no_counter,
                    "GSPS Number": gsps,
                    "Allowed Region": reg
                })
                display_grouped_rows.append({
                    "S.No.": s_no_counter,
                    "MHA NOC Number": noc,
                    "NOC Date": dt,
                    "Organisation": org,
                    "Validity (Yrs)": val if val else "N/A",
                    "GSPS Number": gsps,
                    "Allowed Region": reg
                })
                s_no_counter += 1

            df_sub = pd.DataFrame(sub_table_data)
            st.dataframe(df_sub, hide_index=True, use_container_width=True)
            st.write("")

        st.markdown("---")

        df_export = pd.DataFrame(display_grouped_rows)

        ex_col1, ex_col2 = st.columns(2)
        with ex_col1:
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_export.to_excel(writer, index=False, sheet_name='Grouped Report')
            st.download_button("📊 Download Excel Report", data=output.getvalue(), file_name="GSPS_MHA_Grouped_Report.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        
        with ex_col2:
            pdf_buffer = io.BytesIO()
            doc = SimpleDocTemplate(pdf_buffer, pagesize=landscape(letter), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
            elements = []
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=16, textColor=colors.HexColor('#1f538d'), alignment=1)
            
            elements.append(Paragraph("GSPS MHA Records Grouped Summary Report", title_style))
            elements.append(Paragraph(f"Generated on: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} | Total Unique NOCs: {len(grouped_data)}", styles['Normal']))
            elements.append(Spacer(1, 10))
            
            headers = ["S.No.", "MHA NOC Number", "NOC Date", "Organisation", "Validity", "GSPS Number", "Allowed Region"]
            table_data = [[Paragraph(h, ParagraphStyle('H', parent=styles['Normal'], fontName='Helvetica-Bold', textColor=colors.whitesmoke, alignment=1)) for h in headers]]
            
            for row in display_grouped_rows:
                table_data.append([
                    Paragraph(str(row["S.No."]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                    Paragraph(str(row["MHA NOC Number"]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                    Paragraph(str(row["NOC Date"]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                    Paragraph(str(row["Organisation"]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                    Paragraph(str(row["Validity (Yrs)"]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                    Paragraph(str(row["GSPS Number"]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                    Paragraph(str(row["Allowed Region"]), ParagraphStyle('C', parent=styles['Normal'], fontSize=9, alignment=1)),
                ])
            
            t = Table(table_data, colWidths=[45, 120, 95, 160, 60, 110, 150])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1f538d')),
                ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#aed6f1')),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ]))
            elements.append(t)
            doc.build(elements)
            
            st.download_button("📄 Download PDF Report", data=pdf_buffer.getvalue(), file_name="GSPS_MHA_Grouped_Report.pdf", mime="application/pdf")
    else:
        st.info("No records match current report filters.")


def render_trash_tab():
    st.subheader("Deleted Records Archive")
    cursor.execute("SELECT record_id, mha_noc_number, mha_noc_date, organisation, validity, gsps_number, allowed_region, created_by, modified_by FROM n_records WHERE status = 'DELETED'")
    del_rows = cursor.fetchall()
    
    if del_rows:
        del_data = []
        for r in del_rows:
            del_data.append({
                "Restore": False,
                "Record ID": r[0],
                "NOC Number": r[1],
                "NOC Date": r[2],
                "Organisation": r[3],
                "Validity (Yrs)": r[4],
                "GSPS Number": r[5],
                "Allowed Region": r[6],
                "Created By": r[7],
                "Modified By": r[8]
            })
        df_del = pd.DataFrame(del_data)
        edited_del = st.data_editor(df_del, hide_index=True, use_container_width=True)
        
        to_restore = edited_del[edited_del["Restore"] == True]["Record ID"].tolist()
        if to_restore:
            if st.button("♻️ Restore Selected Records", key="restore_btn_trash"):
                for r_id in to_restore:
                    cursor.execute("UPDATE n_records SET status = 'ACTIVE', modified_by = %s WHERE record_id = %s", (st.session_state.name, r_id))
                st.success(f"Successfully restored {len(to_restore)} record(s)!")
                st.rerun()
    else:
        st.info("Trash archive is empty.")


def render_users_tab():
    st.subheader("User Management & Access Privileges")
    
    if st.session_state.priv_success_msg:
        st.success(st.session_state.priv_success_msg)
        st.session_state.priv_success_msg = ""

    # --- PASSWORD UPDATE SECTION ---
    with st.expander("🔑 Update User Passwords", expanded=False):
        with st.form("update_password_form"):
            cursor.execute("SELECT username, name, role FROM users")
            all_users = cursor.fetchall()
            user_dict = {f"{u[1]} ({u[0]}) - {u[2]}": u[0] for u in all_users}
            
            if st.session_state.role == "Administrator":
                selected_display = st.selectbox("Select User", options=list(user_dict.keys()))
                target_uname = user_dict[selected_display]
            else:
                target_uname = st.session_state.username
                st.info(f"Updating password for your account: **{st.session_state.name} ({target_uname})**")
            
            new_pwd = st.text_input("New Password", type="password")
            confirm_pwd = st.text_input("Confirm New Password", type="password")
            
            update_pwd_btn = st.form_submit_button("🔒 Update Password", use_container_width=True)
            if update_pwd_btn:
                if not new_pwd or not confirm_pwd:
                    st.error("Please fill out both password fields.")
                elif new_pwd != confirm_pwd:
                    st.error("Passwords do not match!")
                else:
                    cursor.execute("UPDATE users SET password = %s WHERE username = %s", (new_pwd.strip(), target_uname))
                    st.success(f"✅ Password for username '{target_uname}' updated successfully!")

    st.markdown("---")
    with st.form("new_user_form"):
        st.write("### Create New User")
        uc1, uc2, uc3, uc4 = st.columns(4)
        with uc1: new_name = st.text_input("Full Name")
        with uc2: new_uname = st.text_input("Username")
        with uc3: new_pwd = st.text_input("Password", type="password")
        with uc4: new_role = st.selectbox("Role", ["Administrator", "Basic User"])
        
        st.write("📌 **Select Tab Access Privileges:**")
        pc1, pc2, pc3, pc4 = st.columns(4)
        with pc1: p_entry = st.checkbox("📝 Record Entry", value=True)
        with pc2: p_reports = st.checkbox("📊 Reports", value=True)
        with pc3: p_trash = st.checkbox("🗑️ Deleted Records", value=True)
        with pc4: p_users = st.checkbox("👤 User Management", value=False)
        
        create_user_btn = st.form_submit_button("➕ Create User", use_container_width=True)
        if create_user_btn:
            if not new_name or not new_uname or not new_pwd:
                st.error("All user fields are required!")
            else:
                try:
                    if new_role == "Administrator":
                        p_users = True

                    cursor.execute(
                        "INSERT INTO users (name, username, password, role, p_entry, p_reports, p_trash, p_users) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (new_name, new_uname, new_pwd, new_role, int(p_entry), int(p_reports), int(p_trash), int(p_users))
                    )
                    st.success(f"User '{new_uname}' created successfully!")
                    st.rerun()
                except psycopg.IntegrityError:
                    st.error(f"Username '{new_uname}' already exists.")

    st.markdown("---")
    st.write("### Existing Users & Privilege Overview")
    cursor.execute("SELECT user_id, name, username, role, p_entry, p_reports, p_trash, p_users FROM users")
    users_rows = cursor.fetchall()
    
    if users_rows:
        users_data = []
        for ur in users_rows:
            is_admin = (ur[3] == "Administrator" or ur[2] == "admin")
            users_data.append({
                "Delete": False,
                "User ID": ur[0],
                "Name": ur[1],
                "Username": ur[2],
                "Role": ur[3],
                "Entry": bool(ur[4]),
                "Reports": bool(ur[5]),
                "Trash": bool(ur[6]),
                "Users": True if is_admin else bool(ur[7])
            })
        df_users = pd.DataFrame(users_data)
        
        edited_users = st.data_editor(
            df_users,
            hide_index=True,
            use_container_width=True,
            disabled=["User ID", "Name", "Username", "Role"],
            key="users_editor"
        )
        
        if st.button("💾 Save Privilege Changes", key="save_privs_btn"):
            validation_error = False
            for idx, row in edited_users.iterrows():
                role = row["Role"]
                uname = row["Username"]
                is_admin = (role == "Administrator" or uname == "admin")
                
                if is_admin and not row["Users"]:
                    validation_error = True
                    break
            
            if validation_error:
                st.error("⚠️ Users cannot be left unchecked for Administrator role account.")
            else:
                for idx, row in edited_users.iterrows():
                    u_id = row["User ID"]
                    uname = row["Username"]
                    role = row["Role"]
                    
                    if uname == 'admin' or role == 'Administrator':
                        e = 1 if row["Entry"] else 0
                        r = 1 if row["Reports"] else 0
                        t = 1 if row["Trash"] else 0
                        us = 1
                    else:
                        e = 1 if row["Entry"] else 0
                        r = 1 if row["Reports"] else 0
                        t = 1 if row["Trash"] else 0
                        us = 1 if row["Users"] else 0

                    cursor.execute(
                        "UPDATE users SET p_entry = %s, p_reports = %s, p_trash = %s, p_users = %s WHERE user_id = %s",
                        (e, r, t, us, u_id)
                    )
                
                cursor.execute("SELECT p_entry, p_reports, p_trash, p_users FROM users WHERE username = %s", (st.session_state.username,))
                p_row = cursor.fetchone()
                if p_row:
                    st.session_state.p_entry = p_row[0]
                    st.session_state.p_reports = p_row[1]
                    st.session_state.p_trash = p_row[2]
                    st.session_state.p_users = 1 if st.session_state.role == 'Administrator' else p_row[3]

                st.session_state.priv_success_msg = "✅ Confirmation: User privileges updated successfully!"
                st.rerun()

        st.write("")
        admin_selected = edited_users[(edited_users["Delete"] == True) & ((edited_users["Role"] == "Administrator") | (edited_users["Username"] == "admin"))]
        
        if not admin_selected.empty:
            st.error("⚠️ Administrator accounts cannot be selected for deletion!")
        
        valid_to_del = edited_users[(edited_users["Delete"] == True) & (edited_users["Role"] != "Administrator") & (edited_users["Username"] != "admin")]
        users_to_del = valid_to_del["Username"].tolist()

        if users_to_del:
            if st.button("🗑️ Delete Selected Users", type="primary", key="del_users_btn"):
                for u in users_to_del:
                    if u == st.session_state.username:
                        st.error("You cannot delete your own active account.")
                    else:
                        cursor.execute("DELETE FROM users WHERE username = %s", (u,))
                st.success("Selected user(s) deleted.")
                st.rerun()


# ==========================================
# DYNAMIC TAB CREATION BASED ON PERMISSIONS
# ==========================================
tab_labels = []
tab_funcs = []

if st.session_state.p_entry == 1:
    tab_labels.append("📝 Data Entry & Management")
    tab_funcs.append(render_entry_tab)
if st.session_state.p_reports == 1:
    tab_labels.append("📊 Reports & Analytics")
    tab_funcs.append(render_reports_tab)
if st.session_state.p_trash == 1:
    tab_labels.append("🗑️ Deleted Records")
    tab_funcs.append(render_trash_tab)
if st.session_state.p_users == 1 or st.session_state.role == "Administrator":
    tab_labels.append("👤 User Management")
    tab_funcs.append(render_users_tab)

if tab_labels:
    active_tabs = st.tabs(tab_labels)
    for idx, func in enumerate(tab_funcs):
        with active_tabs[idx]:
            func()
else:
    st.error("Your app has no tab access privileges enabled. Please contact your system administrator.")
