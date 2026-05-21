import streamlit as st
import google.generativeai as genai
from pathlib import Path
import pandas as pd
from datetime import datetime, timezone, timedelta
import requests
import os
import csv
import markdown
import json
import re
import time
import hashlib
import streamlit.components.v1 as components

# ============================================================================
# 🔒 SECURITY CONFIG - BẢO VỆ API KEY & RATE LIMITING
# ============================================================================
MAX_REQUESTS_PER_HOUR = 5  # Tối đa 5 lần soát xét/giờ
MAX_FILE_SIZE_MB = 10  # Mỗi file PDF tối đa 10MB
ALLOWED_FILE_EXTENSIONS = ['.pdf']
SESSION_TIMEOUT_MINUTES = 20  # Auto logout sau 20 phút không hoạt động

# =========================================
# HÀM QUẢN LÝ LƯỢT SOÁT XÉT (BỘ NHỚ VĨNH CỬU)
# =========================================
QUOTA_FILE = "luot_su_dung.json"

def lay_luot_su_dung(username):
    today_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if username in data and data[username].get("date") == today_str:
                return data[username].get("count", 0)
        except:
            pass
    return 0

def tang_luot_su_dung(username):
    today_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
    data = {}
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            pass
    
    # Nếu đã có data của hôm nay thì +1, nếu sang ngày mới thì reset về 1
    if username in data and data[username].get("date") == today_str:
        data[username]["count"] += 1
    else:
        data[username] = {"date": today_str, "count": 1}
        
    with open(QUOTA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

# --- KHO LƯU TRỮ BÁO CÁO CHO SẾP ---
REPORTS_DIR = "reports_archive"
if not os.path.exists(REPORTS_DIR):
    os.makedirs(REPORTS_DIR)

# --- TẠO VERSION TỰ ĐỘNG THEO FILE APP.PY ---
try:
    file_timestamp = os.path.getmtime(__file__)
    vn_tz = timezone(timedelta(hours=7))
    dt_vn = datetime.fromtimestamp(file_timestamp, vn_tz)
    APP_VERSION = f"v{dt_vn.strftime('%d%m%y')}.{dt_vn.hour}"
except Exception:
    APP_VERSION = "v050526.15"

# --- CẤU HÌNH TRANG ---
st.set_page_config(page_title="Công cụ soát xét giấy phép tần số", layout="wide")

# --- ĐOẠN MÃ CSS ÉP THU HẸP LỀ TRÊN VÀ CHỈNH NÚT ---
st.markdown("""
    <style>
        .block-container { padding-top: 3.5rem !important; }
        [data-testid="stSidebar"] > div:first-child { padding-top: 2rem !important; }
        
        button[kind="primary"] {
            background-color: #007bff !important;
            border-color: #007bff !important;
            color: white !important;
        }
        button[kind="primary"]:hover {
            background-color: #0056b3 !important;
            border-color: #0056b3 !important;
        }
        
        /* Chỉnh lại khoảng cách cho Radio list bên Admin cho gọn */
        .stRadio [role="radiogroup"] {
            gap: 0.5rem;
        }
    </style>
""", unsafe_allow_html=True)

# --- CHÈN BANNER LOGO ---
try:
    st.image("logo_CTS.jpg", use_container_width=True) 
except Exception as e:
    pass  # Không hiển thị error nếu không có logo

# ============================================================================
# 🔧 PARSER & VALIDATOR - SỬA LỖI OUTPUT TỪ GEMINI
# ============================================================================

def extract_json_from_response(response_text):
    """✅ EXTRACT JSON TỪ RESPONSE GEMINI & TỰ ĐỘNG SỬA LỖI CÚ PHÁP"""
    try:
        # Làm sạch chuỗi markdown
        text = response_text.replace('```json', '').replace('```', '').strip()
        
        # Tìm khối JSON
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            json_part = text[start:end+1]
            
            # CỨU HỘ AI: Tự động điền dấu phẩy (,) bị thiếu giữa các cụm data
            json_part = re.sub(r'\}\s*"', '},\n"', json_part)
            json_part = re.sub(r'\]\s*"', '],\n"', json_part)
            
            # CỨU HỘ AI: Xóa dấu phẩy thừa ở cuối (Trailing comma)
            json_part = re.sub(r',\s*\}', '}', json_part)
            json_part = re.sub(r',\s*\]', ']', json_part)
            
            return json.loads(json_part)
        return None
    except json.JSONDecodeError as e:
        st.warning(f"⚠️ Hệ thống đang tự động khắc phục lỗi định dạng AI (Mã lỗi nội bộ: {e})")
        return None
    except Exception as e:
        return None

def validate_classification_response(data):
    """✅ VALIDATE CLASSIFICATION RESPONSE"""
    if not isinstance(data, dict):
        return False, "Response không phải dictionary"
    
    required_fields = ["all_found_licenses", "all_found_organizations"]
    for field in required_fields:
        if field not in data:
            return False, f"Thiếu field: {field}"
    return True, "✅ Valid"

def retry_with_fallback(response_text, field_name="licenses"):
    """🔄 RETRY LOGIC - Nếu Gemini format sai, parse thủ công và LỌC TRÙNG LẶP"""
    try:
        data = extract_json_from_response(response_text)
        if data:
            return data
    except:
        pass
    
    gp_pattern = r'\d{4,8}/GP[-A-Z0-9]*'
    # Lọc dữ liệu duy nhất (Set) ngay từ khâu chữa cháy
    licenses = list(set(re.findall(gp_pattern, response_text.upper())))
    
    return {
        "all_found_licenses": licenses,
        "all_found_organizations": [],
        "pairs": []
    }

def clean_markdown_output(text):
    """🧹 LOẠI BỎ MARKDOWN KHÔNG CẦN THIẾT"""
    text = re.sub(r'```[\w]*\n', '', text)
    text = re.sub(r'```', '', text)
    text = text.replace('\\n', '\n')
    text = text.replace('\\t', '\t')
    return text.strip()

# ============================================================================
# 🔐 HỆ THỐNG BẢO VỆ SESSION & RATE LIMITING
# ============================================================================

def check_session_timeout():
    """🔒 TỰ ĐỘNG LOGOUT sau SESSION_TIMEOUT_MINUTES"""
    if st.session_state.logged_in_user is not None:
        current_time = datetime.now(timezone(timedelta(hours=7)))
        last_activity = st.session_state.get('last_activity_time')
        
        if last_activity:
            time_diff = (current_time - last_activity).total_seconds() / 60
            if time_diff > SESSION_TIMEOUT_MINUTES:
                st.session_state.logged_in_user = None
                st.session_state.request_count_today = 0
                st.rerun()
        
        st.session_state.last_activity_time = current_time

def validate_uploaded_pdf(file_obj):
    """✅ KIỂM TRA FILE PDF CÓ HỢP LỆ KHÔNG"""
    if not file_obj.name.lower().endswith('.pdf'):
        return False, "❌ File phải có định dạng .pdf"
    
    file_size_mb = file_obj.size / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        return False, f"❌ File quá lớn ({file_size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB)"
    
    file_obj.seek(0)
    header = file_obj.read(4)
    file_obj.seek(0)
    
    if header != b'%PDF':
        return False, "❌ File không phải PDF hợp lệ"
    
    return True, "✅ File hợp lệ"

def check_rate_limit():
    """🚦 RATE LIMITING - Tối đa 5 lần/giờ"""
    if 'request_count_today' not in st.session_state:
        st.session_state.request_count_today = 0
    
    if 'last_request_time' not in st.session_state:
        st.session_state.last_request_time = datetime.now(timezone(timedelta(hours=7)))
    
    current_time = datetime.now(timezone(timedelta(hours=7)))
    time_diff = (current_time - st.session_state.last_request_time).total_seconds() / 3600
    
    if time_diff >= 1:
        st.session_state.request_count_today = 0
    
    if st.session_state.request_count_today >= MAX_REQUESTS_PER_HOUR:
        return False, f"⛔ Bạn đã dùng hết {MAX_REQUESTS_PER_HOUR} lần soát xét trong giờ này. Vui lòng thử lại sau."
    
    return True, f"✅ Còn {MAX_REQUESTS_PER_HOUR - st.session_state.request_count_today} lần soát xét"

def log_api_usage(can_bo, danh_sach_file, cost_usd, tokens_used):
    """📊 GHI NHẬT KÝ USAGE CHI TIẾT"""
    log_file = "api_usage_log.csv"
    thoi_gian = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    
    file_exists = os.path.exists(log_file)
    with open(log_file, "a", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Thời gian", "Chuyên viên", "Số file", "Tokens", "Chi phí (USD)", "Chi phí (VNĐ)"])
        
        cost_vnd = cost_usd * 25400
        writer.writerow([
            thoi_gian, 
            can_bo, 
            len(danh_sach_file),
            tokens_used,
            f"{cost_usd:.6f}",
            f"{cost_vnd:,.0f}"
        ])

# ==========================================
# CƠ CHẾ ĐĂNG NHẬP (LẤY TỪ EXCEL)
# ==========================================
if 'logged_in_user' not in st.session_state:
    st.session_state.logged_in_user = None
    st.session_state.last_activity_time = None
    st.session_state.request_count_today = 0

def load_users():
    try:
        df = pd.read_excel("danh_sach_nguoi_dung.xlsx")
        return dict(zip(df['Họ và tên'], df['Mật khẩu'].astype(str)))
    except Exception as e:
        st.error(f"Lỗi đọc file danh_sach_nguoi_dung.xlsx: {e}")
        return {}

users_db = load_users()

# ==========================================
# HÀM CACHE QUY TẮC (TỐI ƯU TOKEN)
# ==========================================
@st.cache_data(ttl=3600)  # Cache 1 giờ
def load_rules_cached():
    """Tải quy tắc file 1 lần rồi tái sử dụng - TIẾT KIỆM 50% TOKEN"""
    rules_text = []
    rules_pdf = []
    
    rules_dir = Path("Rules")
    if not rules_dir.exists():
        rules_dir.mkdir()
    
    local_rule_files = list(rules_dir.glob("*.txt")) + list(rules_dir.glob("*.pdf"))
    
    for filepath in local_rule_files:
        if filepath.suffix.lower() == '.txt':
            with open(filepath, "r", encoding="utf-8") as f:
                rules_text.append(f"--- {filepath.name} ---\n" + f.read())
        else:
            with open(filepath, "rb") as f:
                rules_pdf.append({"mime_type": "application/pdf", "data": f.read()})
    
    return rules_text, rules_pdf, local_rule_files

# ==========================================
# HÀM GHI NHẬT KÝ AN TOÀN - CHỐNG XUNG ĐỘT GHI FILE
# ==========================================
def ghi_nhat_ky_he_thong(can_bo, danh_sach_file, danh_sach_gp=[], danh_sach_to_chuc=[], file_bao_cao="", trang_thai="Thành công", ghi_chu=""):
    log_file = "nhat_ky_tham_dinh.csv"
    thoi_gian = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    
    ten_file_str = " | ".join([str(x) for x in danh_sach_file]) if danh_sach_file else "Không có file"
    so_gp_str = " | ".join([str(x) for x in danh_sach_gp]) if danh_sach_gp else "Không xác định"
    to_chuc_str = " | ".join([str(x) for x in danh_sach_to_chuc]) if danh_sach_to_chuc else "Không xác định"
    
    try:
        file_exists = os.path.exists(log_file)
        with open(log_file, "a", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(["Thời gian", "Cán bộ thẩm định", "Tổ chức/cá nhân", "Số Giấy phép", "Danh sách file tải lên", "File báo cáo", "Trạng thái", "Ghi chú"])
            writer.writerow([thoi_gian, can_bo, to_chuc_str, so_gp_str, ten_file_str, file_bao_cao, trang_thai, ghi_chu])
    except Exception as e:
        st.sidebar.error(f"⚠️ Nhật ký hệ thống tạm thời bận: {str(e)}")

# ==========================================
# ĐIỀU HƯỚNG BẰNG THAM SỐ URL (/?view=admin)
# ==========================================
query_params = st.query_params
view_mode = query_params.get("view", "")

check_session_timeout()

if view_mode != "admin":
    # ==========================================
    # CHẶN BẢO MẬT: GIAO DIỆN ĐĂNG NHẬP
    # ==========================================
    if st.session_state.logged_in_user is None:
        col1, col2, col3 = st.columns([1.4, 1.2, 1.4])
        with col2:
            st.markdown("""
                <p style='text-align: center; font-size: 22px; font-weight: bold; margin-top: 10px; color: #333;'>
                    CÔNG CỤ HỖ TRỢ SOÁT XÉT GP TẦN SỐ
                </p>
            """, unsafe_allow_html=True)
            with st.container(border=True):
                if users_db:
                    selected_user = st.selectbox("👤 Chuyên viên:", options=list(users_db.keys()))
                    entered_pass = st.text_input("🔑 Mật khẩu:", type="password")
                    
                    if st.button("Đăng nhập", use_container_width=True, type="primary"):
                        if entered_pass == users_db[selected_user]:
                            st.session_state.logged_in_user = selected_user
                            st.session_state.last_activity_time = datetime.now(timezone(timedelta(hours=7)))
                            st.session_state.request_count_today = 0
                            st.success(f"Xin chào {selected_user}! Đang tải hệ thống...")
                            st.rerun()
                        else:
                            st.error("Sai mật khẩu! Vui lòng thử lại.")
                else:
                    st.warning("Không tìm thấy danh sách người dùng. Vui lòng kiểm tra lại file Excel.")
        st.stop()

    # ==========================================
    # CÔNG CỤ SOÁT XÉT (GIAO DIỆN CHÍNH)
    # ==========================================
    st.header("Soát xét giấy phép tần số")
    st.caption(f"Phiên bản: {APP_VERSION} | PRO")
    
    with st.sidebar:
        st.markdown(f"**👤 Chuyên viên:** {st.session_state.logged_in_user}")
        st.session_state.request_count_today = lay_luot_su_dung(st.session_state.logged_in_user)
        
        st.sidebar.markdown(f"**📊 Lần soát xét hôm nay:** {st.session_state.request_count_today}/{MAX_REQUESTS_PER_HOUR}")        
        if st.button("🚪 Đăng xuất", use_container_width=True):
            st.session_state.logged_in_user = None
            st.session_state.last_activity_time = None
            st.rerun()
        st.markdown("---")

    st.sidebar.markdown("### Bộ Quy Tắc")
    rules_text, rules_pdf, local_rule_files = load_rules_cached()
    
    if not local_rule_files:
        st.sidebar.error("⚠️ Thư mục 'Rules' đang trống! Vui lòng copy các file quy tắc (PDF) vào thư mục Rules.")
    else:
        st.sidebar.success(f"✅ Đã nạp tự động {len(local_rule_files)} file")
        for name in local_rule_files:
            st.sidebar.caption(f"📄 {name.name}")

    st.sidebar.markdown("---")

    st.sidebar.header("Trạng thái Hệ thống")
    try:
        api_key = st.secrets.get("GEMINI_API_KEY", None)
        if not api_key:
            raise KeyError("GEMINI_API_KEY not found")
        st.sidebar.success("✅ Đã kết nối API Key!")
    except (KeyError, Exception) as e:
        st.sidebar.error("⚠️ Không tìm thấy API Key trong file secrets.toml")
        st.stop()

    try:
        genai.configure(api_key=api_key)
        model_name = "models/gemini-2.5-flash"
        st.sidebar.success(f"✅ Model: {model_name.replace('models/', '')}")
        model = genai.GenerativeModel(model_name)
    except Exception as e:
        st.sidebar.error(f"Lỗi khởi tạo cấu hình: {e}")
        st.stop()

    if 'bao_cao_tham_dinh' not in st.session_state:
        st.session_state.bao_cao_tham_dinh = None
    if 'thong_ke_chi_phi' not in st.session_state:
        st.session_state.thong_ke_chi_phi = None
    if 'hoso_uploader_key' not in st.session_state:
        st.session_state.hoso_uploader_key = 0 

    # ==========================================
    # KHU VỰC TẢI HỒ SƠ & THẨM ĐỊNH
    # ==========================================
    
    st.info("""
    **📘 Hướng dẫn sử dụng nhanh:**
    1. Xuất các file giấy phép và thông báo phí ra dạng Pdf (từ phần mềm cấp phép của Cục). Nếu có nhiều giấy phép của cùng 1 đơn vị thì dùng chức năng xuất nhiều giấy phép ra 01 file pdf. Các file bản khai lấy trên cổng DVC. Chọn đúng loại hồ sơ để soát xét và bấm nút upload để tải các file lên.
    2. Bấm **"Bắt đầu soát xét"** để rà soát hồ sơ và đợi hoàn thành (mất từ 1-3 phút). Kết quả được tự động lưu và Trưởng phòng có thể xem được luôn qua mạng. 
    ⚠️ Lưu ý: Chức năng soát xét thông báo phí chỉ để tham khảo. Mỗi lần chỉ nên tải hồ sơ của **01 đơn vị** (< **10MB**).
    """)
    st.subheader(" Tải lên các bộ hồ sơ đối soát (giấy phép, bản khai, TBP) dạng pdf")
    
    with st.container(border=True):
        # Ép khoảng lề dưới của tiêu đề nhỏ lại (chỉ còn 5px)
        st.markdown("<p style='margin-bottom: 5px;'><b>1. Chọn chế độ soát xét:</b></p>", unsafe_allow_html=True)
        che_do_soat_xet = st.radio(
            "Chế độ:",
            options=["Soát xét đơn lẻ (01 GP/1 hô sơ)", "Soát xét theo lô (Tối đa 10 GP/1 hồ sơ)"],
            horizontal=True,
            key=f"che_do_{st.session_state.hoso_uploader_key}",
            label_visibility="collapsed"
        )
        
        # Thay thế st.markdown("---") bằng thẻ <hr> có margin âm để hút đường kẻ lên trên
        st.markdown("<hr style='margin-top: -10px; margin-bottom: 15px; border-top: 1px solid #e6e6e6;'>", unsafe_allow_html=True)
        
        col_combo, col_upload = st.columns([3.5, 6.5])
        
        with col_combo:
            loai_hoso_nghiep_vu = st.selectbox(
                "2. Chọn loại hồ sơ (quy tắc):",
                options=[
                    "--- Vui lòng chọn ---",
                    "Hồ sơ giấy cấp mới giấy phép",
                    "Hồ sơ chỉ gia hạn giấy phép",
                    "Hồ sơ gia hạn và sửa đổi giấy phép",
                    "Hồ sơ chỉ sửa đổi giấy phép"
                ],
                key=f"loai_hoso_{st.session_state.hoso_uploader_key}"
            )
            
        with col_upload:
            uploaded_files = st.file_uploader(
                "3. Tải lên file PDF", 
                type=['pdf'], 
                accept_multiple_files=True,
                key=f"documents_{st.session_state.hoso_uploader_key}",
                label_visibility="collapsed"
            )

    hoso_count = len(uploaded_files) if uploaded_files else 0
    hoso_size_mb = sum(f.size for f in uploaded_files) / (1024*1024) if uploaded_files else 0

    btn_col1, btn_col2, btn_col3 = st.columns([2.5, 2.5, 5])
    with btn_col1:
        start_btn = st.button(" Bắt đầu soát xét", type="primary", use_container_width=True)
    with btn_col2:
        if st.button("🔄 Reset hồ sơ", use_container_width=True):
            st.session_state.hoso_uploader_key += 1
            st.session_state.bao_cao_tham_dinh = None
            st.session_state.thong_ke_chi_phi = None
            st.rerun()

    if start_btn:
        can_request, rate_msg = check_rate_limit()
        st.info(rate_msg)
        
        if not can_request:
            st.error("❌ Bạn đã vượt giới hạn soát xét trong giờ này!")
            st.stop()
            
        if loai_hoso_nghiep_vu == "--- Vui lòng chọn ---":
            st.error("⚠️ Vui lòng chọn đúng dạng hồ sơ nghiệp vụ trước khi tiến hành đối soát.")
            st.stop()
        
        if hoso_size_mb > 10:
            st.error(f"⛔ Vượt quá dung lượng cho phép! (Hồ sơ của bạn {hoso_size_mb:.1f}MB, tối đa là 10MB). Vui lòng bấm 'Reset hồ sơ' hoặc nhấn dấu X gỡ bớt để tiếp tục.")
            st.stop()
        elif not uploaded_files:
            st.error("⚠️ Vui lòng tải lên các file hồ sơ cần soát xét.")
            st.stop()
        elif "mainrules.txt" not in [f.name.lower() for f in local_rule_files]:
            st.error("⛔ THIẾU BỘ QUI TẮC GỐC: Thư mục 'Rules' bắt buộc phải chứa file có tên 'mainrules.txt'!")
            st.stop()
        else:
            all_valid = True
            validation_errors = []
            
            for f in uploaded_files:
                is_valid, msg = validate_uploaded_pdf(f)
                if not is_valid:
                    all_valid = False
                    validation_errors.append(f"**{f.name}**: {msg}")
            
            if not all_valid:
                st.error("❌ Một số file không hợp lệ:\n" + "\n".join(validation_errors))
                st.stop()
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            total_input_tokens = 0
            total_output_tokens = 0
            
            with st.spinner("AI đang truy xuất qui tắc từ hệ thống và tiến hành đối soát..."):
                try:
                    status_text.info("📖 Bước 1: Nạp tự động bộ qui tắc...")
                    progress_bar.progress(15)
                    
                    files_data = [{"mime_type": "application/pdf", "data": f.getvalue()} for f in uploaded_files]

                    timestamp_file = datetime.now(timezone(timedelta(hours=7))).strftime("%Y%m%d_%H%M%S")
                    org_name_file = "Không xác định" 
                    report_filename = f"{timestamp_file}_{loai_hoso_nghiep_vu.replace(' ', '_')}.html"
                    
                    status_text.info("📑 Bước 2: Quét nội dung văn bản để phân loại...")
                    progress_bar.progress(35)
                    
                    classification_prompt = """Bạn là chuyên gia trích xuất dữ liệu siêu chính xác tại Cục Tần số. Hãy đọc TOÀN BỘ các file tài liệu (quét qua từng file một) và trích xuất thông tin:

1. TRÍCH XUẤT SỐ GIẤY PHÉP (LUẬT THÉP CẦN PHÂN BIỆT RÕ):
   - BƯỚC NHẬN DIỆN FILE: Bạn phải nhìn vào tiêu đề lớn trên cùng của tài liệu. 
     + NẾU là file có chữ "GIẤY PHÉP SỬ DỤNG TẦN SỐ VÀ THIẾT BỊ VÔ TUYẾN ĐIỆN": ĐÂY LÀ FILE ĐÚNG. Hãy nhìn lên góc trên cùng bên trái (dưới chữ CỤC TẦN SỐ), lấy chính xác chuỗi ký tự sau chữ "Số: " (Ví dụ: "333555/GP-GH").
     + NẾU là file có chữ "Thông báo Phí, lệ phí tần số vô tuyến điện": NGHIÊM CẤM TRÍCH XUẤT SỐ HIỆU CỦA FILE NÀY (VD: Tuyệt đối bỏ qua các số dạng 797/26/CTS-DR).
   - VÙNG CẤM TRONG VĂN BẢN: Tuyệt đối KHÔNG trích xuất các số giấy phép cũ được nhắc đến trong phần thân nội dung văn bản (đặc biệt là ở mục Các quy định khác hoặc câu "Thay giấy phép số...").
   - Bắt buộc quét đủ tất cả các file Giấy phép thực sự và đưa vào mảng.
2. TRÍCH XUẤT THỜI HẠN GIẤY PHÉP: Lấy ngày tại mục 'Có giá trị đến hết ngày ....' trên file Giấy phép.
3. SỐ GIẤY PHÉP SỬA ĐỔI (Ở Bản Khai): Nhìn dưới dòng 'Bản khai thông số kỹ thuật, khai thác...', tìm câu 'sửa đổi, bổ sung cho giấy phép số [Số_Giấy_Phép]'. Quét đọc và lấy chính xác chuỗi số đó. KHÔNG CẦN QUAN TÂM Ô TÍCH (☑/☐). Nếu để trống, trả về "".

⚠️ OUTPUT BẮT BUỘC CHỈ LÀ MỘT CHUỖI JSON CHUẨN XÁC THEO ĐÚNG CẤU TRÚC VÍ DỤ DƯỚI ĐÂY (Tuyệt đối không sai dấu phẩy, không chứa markdown):
{
  "all_found_licenses": ["333555/GP-GH", "333555/GP-GH2"],
  "license_dates": {
    "333555/GP-GH": "07/07/2026",
    "333555/GP-GH2": "07/07/2029"
  },
  "modified_license_number": "300872/GP-GH",
  "all_found_organizations": ["Tên tổ chức ở đây"]
}"""
                    
                    json_config = genai.GenerationConfig(response_mime_type="application/json")
                    response_classify = model.generate_content(
                        [classification_prompt] + files_data, 
                        generation_config=json_config
                    )
                    
                    if response_classify.usage_metadata:
                        total_input_tokens += response_classify.usage_metadata.prompt_token_count or 0
                        total_output_tokens += response_classify.usage_metadata.candidates_token_count or 0
                    
                    classification_result = extract_json_from_response(response_classify.text)
                    if not classification_result:
                        classification_result = retry_with_fallback(response_classify.text)
                        
                    # LỌC TRÙNG LẶP DỮ LIỆU & FIX LỖI KHOẢNG TRẮNG ẨN TRƯỚC DẤU '/'
                    raw_licenses = classification_result.get("all_found_licenses", []) if classification_result else []
                    raw_licenses = list(set([re.sub(r'\s+/', '/', str(x)).upper().strip() for x in raw_licenses]))
                    
                    # --- CHỐT CHẶN AN TOÀN: ĐẾM SỐ LƯỢNG GP DỰA TRÊN SỐ HIỆU GỐC ---
                    # --- CHỐT CHẶN AN TOÀN: ĐẾM SỐ LƯỢNG GPDỰA TRÊN SỐ HIỆU GỐC ---
                    def dem_so_hieu_goc(gp_str):
                        match = re.search(r'(\d+)', str(gp_str))
                        return match.group(1) if match else ""
                        
                    unique_base_licenses = set([dem_so_hieu_goc(x) for x in raw_licenses if dem_so_hieu_goc(x)])
                    so_luong_tram = len(unique_base_licenses)
                    
                    if "lô" in che_do_soat_xet.lower():
                        if so_luong_tram > 10:
                            error_message = f"⛔ VƯỢT QUÁ GIỚI HẠN LÔ: File của bạn chứa tới {so_luong_tram} trạm/giấy phép khác nhau. Để đảm bảo độ chính xác và tránh AI bị 'tẩu hỏa nhập ma', hệ thống chỉ cho phép xử lý tối đa 10 GPcùng lúc. Vui lòng tách file PDF ra nhỏ hơn!"
                            st.error(error_message)
                            st.stop()
                        elif so_luong_tram == 1:
                            error_message = f"⛔ SAI CHẾ ĐỘ: Bạn đang chọn 'Soát xét theo lô' nhưng hệ thống chỉ phát hiện có đúng {so_luong_tram} trạm/giấy phép trong file tải lên. Đã là lô thì phải có từ 02 GPtrở lên. Vui lòng chọn chế độ 'Soát xét đơn lẻ'!"
                            st.error(error_message)
                            st.stop()
                        
                    if "đơn lẻ" in che_do_soat_xet.lower() and so_luong_tram > 1:
                        error_message = f"⛔ SAI CHẾ ĐỘ: Bạn đang chọn 'Soát xét đơn lẻ' nhưng hệ thống phát hiện có tới {so_luong_tram} GPkhác nhau trong file tải lên. Vui lòng chọn 'Soát xét theo lô' hoặc chỉ tải lên 01 bộ hồ sơ duy nhất!"
                        st.error(error_message)
                        st.stop()
                    
                    license_dates = classification_result.get("license_dates", {}) if classification_result else {}
                    modified_license_number = classification_result.get("modified_license_number", "").strip() if classification_result else ""
                    danh_sach_to_chuc = classification_result.get("all_found_organizations", []) if classification_result else []
                    danh_sach_ten_file = [f.name for f in uploaded_files]

                    if danh_sach_to_chuc:
                        org_name_file = danh_sach_to_chuc[0]
                        clean_org_name_file = re.sub(r'[\\/*?:"<>|]', "", org_name_file)[:50].strip()
                        report_filename = f"{timestamp_file}_{clean_org_name_file}.html"

                    error_message = None
                    
                    # --- HÀM TÍNH ĐIỂM HẬU TỐ ---
                    def lay_cap_do_duoi(gp_str):
                        gp_str = str(gp_str).strip().upper()
                        parts = gp_str.split('/')
                        if len(parts) < 2:
                            return 0
                        
                        suffix = parts[-1]
                        score = 0
                        
                        match_gh = re.search(r'GH(\d*)', suffix)
                        if match_gh:
                            num = match_gh.group(1)
                            score = int(num) if num else 1
                            
                        if 'SĐ' in suffix or 'SD' in suffix:
                            score += 0.5 
                            
                        return score

                    # --- KIỂM TRA LOGIC NGHIỆP VỤ (VÔ HIỆU HÓA RÀO CẢN ĐẾM FILE) ---
                    # --- KIỂM TRA LOGIC 1: HỒ SƠ GIẤY PHÉP CẤP MỚI ---
                    if loai_hoso_nghiep_vu == "Hồ sơ giấy cấp mới giấy phép":
                        if "đơn lẻ" in che_do_soat_xet.lower() and hoso_count < 2:
                            error_message = "❌ CẤP MỚI THẤT BẠI: Thiếu file hồ sơ thành phần. Quy trình cấp mới yêu cầu bắt buộc phải cung cấp đủ tối thiểu 01 file Giấy phép và 01 file Bản khai tương ứng."
                        else:
                            for gp_num in raw_licenses:
                                gp_upper = str(gp_num).upper().strip()
                                if "GH" in gp_upper or not (gp_upper.endswith('/GP') or gp_upper.endswith('/GP-DP')):
                                    error_message = f"❌ SAI NGHIỆP VỤ HỒ SƠ: Hệ thống phát hiện văn bản tải lên là ({gp_num}), không phải cấu trúc Giấy phép Cấp mới thuần túy (phải có đuôi /GP hoặc /GP-DP)."
                                    break

                    # --- KIỂM TRA LOGIC 2: HỒ SƠ CHỈ GIA HẠN ---
                    elif loai_hoso_nghiep_vu == "Hồ sơ chỉ gia hạn giấy phép":
                        if "đơn lẻ" in che_do_soat_xet.lower():
                            if hoso_count < 3:
                                error_message = "❌ GIA HẠN THẤT BẠI: Thiếu file hồ sơ thành phần. Diện chỉ gia hạn bắt buộc phải cung cấp đủ tối thiểu 02 file Giấy phép và 01 file Bản khai đề nghị gia hạn."
                            elif len(raw_licenses) < 2:
                                error_message = "❌ THIẾU VĂN BẢN ĐỐI CHIẾU: Không tìm thấy đủ dữ liệu số hiệu văn bản của cả 02 file Giấy phép trên hệ thống để thực hiện rà soát."
                            else:
                                cac_cap_do = sorted(list(set([lay_cap_do_duoi(x) for x in raw_licenses])))
                                hop_le_lien_ke = False
                                if len(cac_cap_do) >= 2:
                                    for i in range(len(cac_cap_do) - 1):
                                        if cac_cap_do[i+1] - cac_cap_do[i] == 1:
                                            hop_le_lien_ke = True
                                            break
                                if not hop_le_lien_ke:
                                    error_message = f"❌ SAI QUY TẮC LIỀN KỀ: 02 Giấy phép tải lên không có hậu tố kề nhau liền mạch. Nội dung quét thực tế đang là: {', '.join([str(x) for x in raw_licenses])}."

                    # --- KIỂM TRA LOGIC 3: HỒ SƠ GIA HẠN VÀ SỬA ĐỔI GIẤY PHÉP ---
                    elif loai_hoso_nghiep_vu == "Hồ sơ gia hạn và sửa đổi giấy phép":
                        if "đơn lẻ" in che_do_soat_xet.lower():
                            if hoso_count < 4:
                                error_message = "❌ GIA HẠN & SỬA ĐỔI THẤT BẠI: Thiếu cấu phần tệp tin. Yêu cầu bắt buộc phải cung cấp ít nhất 04 file (02 Giấy phép kề nhau, 01 Bản khai gia hạn, 01 Bản khai thông số kỹ thuật)."
                            elif len(raw_licenses) < 2:
                                error_message = "❌ THIẾU VĂN BẢN ĐỐI CHIẾU: Không quét đủ số hiệu của 02 Giấy phép kỳ kề cận."
                            else:
                                valid_licenses = list(set([x for x in raw_licenses if lay_cap_do_duoi(x) != -1]))
                                sorted_licenses = sorted(valid_licenses, key=lay_cap_do_duoi)
                                cac_cap_do = [lay_cap_do_duoi(x) for x in sorted_licenses]
                                hop_le_lien_ke = False
                                old_license = ""
                                if len(cac_cap_do) >= 2:
                                    for i in range(len(cac_cap_do) - 1):
                                        if cac_cap_do[i+1] - cac_cap_do[i] == 1:
                                            hop_le_lien_ke = True
                                            old_license = sorted_licenses[i]  # Giấy phép kỳ trước
                                            break
                                            
                                def lay_so_hieu_goc(gp_str):
                                    match = re.search(r'(\d+)', str(gp_str))
                                    return match.group(1) if match else ""
                                    
                                base_modified = lay_so_hieu_goc(modified_license_number)
                                base_old = lay_so_hieu_goc(old_license)
                                            
                                if not hop_le_lien_ke:
                                    error_message = f"❌ SAI QUY TẮC LIỀN KỀ: Cặp Giấy phép của diện kết hợp Gia hạn & Sửa đổi bắt buộc phải có hậu tố kề nhau liền mạch. Thực tế quét: {', '.join([str(x) for x in raw_licenses])}."
                                elif not modified_license_number:
                                    error_message = "❌ THIẾU DẤU VẾT NGHIỆP VỤ: Không tìm thấy nội dung ghi số giấy phép tại mục 'sửa đổi, bổ sung cho giấy phép số...' dưới tiêu đề Bản khai thông số kỹ thuật."
                                elif base_modified != base_old:
                                    error_message = f"❌ SAI LỆCH SỐ GIẤY PHÉP SỬA ĐỔI: Số giấy phép gốc ghi trong Bản khai là ({modified_license_number}), không trùng khớp với dãy số gốc của giấy phép hệ thống ({old_license})."

                    # --- KIỂM TRA LOGIC 4: HỒ SƠ CHỈ SỬA ĐỔI GIẤY PHÉP ---
                    elif loai_hoso_nghiep_vu == "Hồ sơ chỉ sửa đổi giấy phép":
                        if "đơn lẻ" in che_do_soat_xet.lower():
                            if hoso_count < 2:
                                error_message = "❌ SỬA ĐỔI THẤT BẠI: Thiếu hồ sơ thành phần. Diện chỉ sửa đổi bắt buộc phải cung cấp đủ tối thiểu 01 file Giấy phép hiện tại và 01 file Bản khai thông số kỹ thuật."
                            
                            valid_licenses = list(set([x for x in raw_licenses if lay_cap_do_duoi(x) != -1]))
                            
                            def lay_so_hieu_goc(gp_str):
                                match = re.search(r'(\d+)', str(gp_str))
                                return match.group(1) if match else ""
                                
                            if not error_message and not modified_license_number:
                                error_message = "❌ THIẾU DẤU VẾT NGHIỆP VỤ: Không tìm thấy nội dung ghi số giấy phép tại mục 'sửa đổi, bổ sung cho giấy phép số...' dưới tiêu đề Bản khai thông số kỹ thuật."
                            elif not error_message and valid_licenses:
                                sorted_licenses = sorted(valid_licenses, key=lay_cap_do_duoi)
                                current_license = sorted_licenses[-1]
                                
                                if lay_so_hieu_goc(modified_license_number) != lay_so_hieu_goc(current_license):
                                    error_message = f"❌ SAI LỆCH SỐ GIẤY PHÉP SỬA ĐỔI: Số giấy phép gốc ghi trong Bản khai là ({modified_license_number}), không khớp với số gốc của giấy phép hiện tại tải lên là ({current_license})."

                            if not error_message and len(license_dates) >= 2:
                                unique_dates = set(license_dates.values())
                                if len(unique_dates) > 1:
                                    error_message = f"❌ SAI QUY TRÌNH SỬA ĐỔI: Mục 'Có giá trị đến hết ngày ....' của văn bản không trùng khớp đồng nhất với nhau. Nghiệp vụ chỉ sửa đổi yêu cầu thời hạn giấy phép gốc phải giữ nguyên giống nhau."

                    if error_message:
                        ghi_nhat_ky_he_thong(
                            can_bo=st.session_state.logged_in_user,
                            danh_sach_file=danh_sach_ten_file,
                            danh_sach_gp=raw_licenses,
                            danh_sach_to_chuc=danh_sach_to_chuc,
                            file_bao_cao=report_filename,
                            trang_thai="Bị chặn đầu vào",
                            ghi_chu=error_message
                        )
                        st.error(error_message)
                        st.warning("⚠️ Hệ thống dừng soát xét")
                        st.stop()

                    status_text.info("🔍 Bước 3: Khởi chạy mô hình AI đối soát chi tiết giấy phép...")
                    progress_bar.progress(70)
                    
                    # Thiết lập câu lệnh điều kiện dựa vào lựa chọn trên màn hình
                    che_do_text = "(CHẾ ĐỘ XỬ LÝ LÔ - BATCH PROCESSING)" if "lô" in che_do_soat_xet else "(CHẾ ĐỘ XỬ LÝ ĐƠN LẺ 1 BỘ HỒ SƠ)"
                    
                    luat_ghep_cap = """
                    1. NGUYÊN TẮC GHÉP CẶP VÀ XỬ LÝ HÀNG LOẠT:
                    - Tài liệu tải lên là DẠNG LÔ. Bạn BẮT BUỘC quét từ trên xuống dưới, tự động nhóm các trang thành TỪNG CẶP hồ sơ dựa theo "Số hiệu gốc" để đối soát (Ví dụ: ghép trang có 386533/GP-GH với 386533/GP).
                    - Hãy liệt kê kết quả rành mạch cho từng GP một.""" if "lô" in che_do_soat_xet else """
                    1. NGUYÊN TẮC XỬ LÝ ĐƠN LẺ:
                    - Đây là 01 bộ hồ sơ độc lập duy nhất. Hãy tập trung 100% công lực để đối chiếu chéo các thông tin của GP này."""

                    audit_prompt = f"""BẠN LÀ CHUYÊN GIA SOÁT XÉT ĐỘC LẬP TẠI CỤC TẦN SỐ VÔ TUYẾN ĐIỆN.
                    DIỆN NGHIỆP VỤ PHÁP LÝ ĐANG ĐỐI SOÁT: {loai_hoso_nghiep_vu} {che_do_text}

                    {luat_ghep_cap}

                    2. NGUYÊN TẮC NGHIỆP VỤ TỐI CAO (SỬ DỤNG BỘ QUY TẮC GỐC MAINRULES):
                    - Toàn bộ các bước nghiệp vụ, cách thức so sánh, quy định đếm tần số, tính lệ phí, và thuật toán đối chiếu BẮT BUỘC phải tuân thủ 100% theo các hướng dẫn trong file 'mainrules.txt'.
                    - Bạn BẮT BUỘC phải tìm đúng phần hướng dẫn tương ứng với "{loai_hoso_nghiep_vu}" trong file 'mainrules.txt' và thực hiện một cách tuần tự, máy móc, chính xác.
                    - Đặc biệt với hồ sơ Gia hạn / Sửa đổi: Tuyệt đối tuân thủ quy tắc so sánh loại trừ các mục 7, 8, 10 trên Bản khai đối với Giấy phép có hậu tố lớn nhất.

                    3. QUY TẮC SO KHỚP VÀ TRUY XUẤT CHUNG (LUẬT THÉP):
                    - "CN" = "Chi nhánh", "TCT" = "Tổng công ty", "CP" = "Cổ phần", "TNHH" = "Trách nhiệm hữu hạn". 
                    - Bỏ qua viết hoa/viết thường. Tự động loại bỏ số 0 vô nghĩa ở đuôi.

                    4. XỬ LÝ VỚI THÔNG BÁO PHÍ (LUẬT THÉP TUYỆT ĐỐI):
                    - TRƯỜNG HỢP 1: NẾU TRONG TẬP HỒ SƠ KHÔNG CÓ FILE "THÔNG BÁO PHÍ": Bạn TUYỆT ĐỐI KHÔNG ĐƯỢC trích dẫn luật, không giải thích công thức tính phí, không lập bảng biểu. Bạn BẮT BUỘC phải im lặng và chỉ được phép in đúng 1 câu duy nhất: "Lưu ý: Không có file Thông báo phí được cung cấp để đối soát trực tiếp các giá trị phí, lệ phí đã ban hành."
                    - TRƯỜNG HỢP 2: NẾU CÓ file Thông báo phí nhưng xuất hiện chữ "Hoàn phí" hoặc "Bù trừ": TUYỆT ĐỐI KHÔNG tính toán. Bắt buộc in câu cảnh báo: "- **Phần hoàn phí, bù trừ phí:** Soát xét thủ công vì tính chất phức tạp."
                    - TRƯỜNG HỢP 3: NẾU CÓ Thông báo phí bình thường: Đối soát và diễn giải ngắn gọn dòng tiền.

                    5. QUY TẮC TRÌNH BÀY BÁO CÁO (LUẬT THÉP VỀ SỰ NGẮN GỌN):
                    - ⚠️ LỆNH CẤM LIỆT KÊ THÔNG SỐ KHỚP: Tuyệt đối KHÔNG ĐƯỢC in ra bất kỳ thông số nào đã khớp nhau (Ví dụ: CẤM in các dòng như "✅ Tên/mã trạm: Khớp", "✅ Địa điểm: Trùng khớp"). 
                    - Nguyên tắc làm việc của bạn là: Hễ thông số đúng thì phải IM LẶNG hoàn toàn về nó. Bạn chỉ lên tiếng khi phát hiện ra lỗi.
                    - ⚠️ QUY ĐỊNH TRÍCH DẪN VỊ TRÍ VĂN BẢN: Tuyệt đối KHÔNG ĐƯỢC sử dụng số trang cộng dồn liên tục của toàn cục bộ hồ sơ (như Trang 11, Trang 12...). BẮT BUỘC phải ghi rõ định dạng: [Tên file PDF gốc chính xác] - tại Trang số [Số trang thực tế của riêng file đó]. Ví dụ: "(Trang 1 của file 374745bksuadoi.pdf)".

                    FORMAT BÁO CÁO BẮT BUỘC (Lặp lại khung này cho từng trạm nếu là chế độ xử lý Lô):
                    ## Kết quả đối soát: [Ghi rõ số hiệu gốc của trạm đang xét, ví dụ: Trạm 374745]
                    - **Trạng thái chung:** [✅ Hồ sơ hợp lệ / ❌ Phát hiện sai lệch]
                    
                    ### 1. Chi tiết sai lệch thông số kỹ thuật
                    [NẾU TẤT CẢ ĐỀU KHỚP NHAU, BẠN CHỈ ĐƯỢC IN ĐÚNG 1 CÂU NÀY VÀ DỪNG LẠI: "✅ Không phát hiện sai lệch thông số kỹ thuật."]
                    [NẾU CÓ SAI LỆCH, CHỈ LIỆT KÊ ĐÚNG CÁC MỤC BỊ SAI THEO MẪU SAU:]
                    - ❌ **[Tên tham số]:** [Nội dung sai lệch giải thích ngắn gọn].

                    ### 2. Đối soát chi tiết Thông báo phí và Các thông tin khác
                    [NẾU KHÔNG CÓ FILE THÔNG BÁO PHÍ: Bắt buộc chỉ in đúng 1 dòng: "Lưu ý: Không có file Thông báo phí được cung cấp để đối soát trực tiếp các giá trị phí, lệ phí đã ban hành." và NGHIÊM CẤM in thêm bất kỳ bảng biểu, diễn giải luật lệ nào khác].
                    [NẾU CÓ FILE THÔNG BÁO PHÍ: Diễn giải dòng tiền. Nếu có bù trừ, in cảnh báo ở Mục 4].
                    ---"""
                    
                    response_audit = model.generate_content(
                        [audit_prompt] + rules_text + rules_pdf + files_data
                    )
                    
                    if response_audit.usage_metadata:
                        input_2 = response_audit.usage_metadata.prompt_token_count or 0
                        output_2 = response_audit.usage_metadata.candidates_token_count or 0
                        total_input_tokens += input_2
                        total_output_tokens += output_2
                    
                    audit_text = clean_markdown_output(response_audit.text)
                    
                    PRICE_INPUT_USD_PER_MILLION = 0.075
                    PRICE_OUTPUT_USD_PER_MILLION = 0.30
                    RATE_USD_TO_VND = 26900
                    
                    cost_input_usd = (total_input_tokens / 1_000_000) * PRICE_INPUT_USD_PER_MILLION * 10
                    cost_output_usd = (total_output_tokens / 1_000_000) * PRICE_OUTPUT_USD_PER_MILLION * 10
                    total_cost_usd = cost_input_usd + cost_output_usd
                    total_cost_vnd = total_cost_usd * RATE_USD_TO_VND

                    st.session_state.bao_cao_tham_dinh = audit_text
                    st.session_state.thong_ke_chi_phi = {
                        "in_tokens": total_input_tokens,
                        "out_tokens": total_output_tokens,
                        "cost_usd": total_cost_usd,
                        "cost_vnd": total_cost_vnd
                    }

                    # --- LƯU CÁC BIẾN META VÀO SESSION STATE ĐỂ DÙNG KHI BẤM NÚT GỬI ---
                    st.session_state.report_filename = report_filename
                    st.session_state.org_name_file = org_name_file
                    st.session_state.raw_licenses = raw_licenses
                    st.session_state.danh_sach_to_chuc = danh_sach_to_chuc
                    st.session_state.danh_sach_ten_file = danh_sach_ten_file

                    # (ĐÃ XÓA KHỐI GHI FILE HTML VÀ GHI NHẬT KÝ HỆ THỐNG TẠI ĐÂY)

                    log_api_usage(
                        can_bo=st.session_state.logged_in_user,
                        danh_sach_file=danh_sach_ten_file,
                        cost_usd=total_cost_usd,
                        tokens_used=total_input_tokens + total_output_tokens
                    )
                    
                    tang_luot_su_dung(st.session_state.logged_in_user)
                    st.session_state.request_count_today = lay_luot_su_dung(st.session_state.logged_in_user)
                    st.session_state.last_request_time = datetime.now(timezone(timedelta(hours=7)))

                    progress_bar.progress(100)
                    status_text.success("✅ Đã hoàn thành soát xét! Vui lòng nhập ý kiến chuyên viên ở mục bên dưới để gửi báo cáo.")

                except Exception as e:
                    st.error(f"❌ Lỗi: {str(e)}")
                    import traceback
                    st.code(traceback.format_exc(), language="python")

    # ==========================================
    # HIỂN THỊ KẾT QUẢ TỪ BỘ NHỚ PHIÊN
    # ==========================================
    if st.session_state.bao_cao_tham_dinh:
        st.markdown("### 📋 Báo cáo soát xét chi tiết")
        
        # Thu thập dữ liệu thông tin hành chính để hiển thị
        ten_don_vi = st.session_state.get('org_name_file', 'Không xác định')
        thoi_gian_soat = datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M:%S")
        
        # --- BỔ SUNG: HIỂN THỊ THÔNG TIN CHUYÊN VIÊN, ĐƠN VỊ TRÊN GIAO DIỆN MÀN HÌNH ---
        st.markdown(f"""
        <div style="background-color: #f8f9fa; padding: 16px; border-left: 5px solid #007bff; border-radius: 4px; margin-bottom: 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <p style="margin: 5px 0; font-size: 16px; color: #333;"><b>👤 Chuyên viên soát xét:</b> {st.session_state.logged_in_user}</p>
            <p style="margin: 5px 0; font-size: 16px; color: #333;"><b>🏢 Tên đơn vị:</b> {ten_don_vi}</p>
            <p style="margin: 5px 0; font-size: 16px; color: #333;"><b>⏰ Thời gian soát xét:</b> {thoi_gian_soat}</p>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown(st.session_state.bao_cao_tham_dinh)
        
        st.markdown("---")
        # Textbox cho chuyên viên nhập ý kiến
        y_kien = st.text_area("✍️ Ý kiến của chuyên viên soát xét:", placeholder="Ví dụ: Đã kiểm tra, sửa lỗi sai lệch địa chỉ theo đúng hồ sơ gốc...", height=100)
        
        # Tích hợp bảng vào markdown
        html_body = markdown.markdown(st.session_state.bao_cao_tham_dinh, extensions=['tables'])
        
        # --- ĐIỀU CHỈNH: PHÓNG TO FONT CHỮ MỤC Ý KIẾN CHUYÊN VIÊN TRONG FILE BÁO CÁO ---
        if y_kien.strip():
            html_body += f"""
            <div style="margin-top: 35px; padding: 22px; background-color: #e8f4f8; border-left: 6px solid #0056b3; border-radius: 4px;">
                <h2 style="color: #0056b3; margin-top: 0; font-size: 24px; font-weight: bold; letter-spacing: 0.5px; border-bottom: none; padding-bottom: 0;">Ý KIẾN CỦA CHUYÊN VIÊN SOÁT XÉT</h2>
                <p style="white-space: pre-wrap; font-size: 19px; color: #111; line-height: 1.6; font-weight: bold; margin-bottom: 0; margin-top: 10px;">{y_kien}</p>
            </div>
            """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Báo Cáo Thẩm Định - Cục Tần số</title>
            <style>
                body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; padding: 40px; max-width: 900px; margin: auto; color: #222; }}
                h1 {{ color: #004494; text-align: center; border-bottom: 2px solid #004494; padding-bottom: 15px; text-transform: uppercase; font-size: 24px; }}
                h2 {{ color: #d9534f; margin-top: 30px; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
                ul {{ list-style-type: none; padding-left: 0; }}
                li {{ margin-bottom: 10px; padding: 10px; background-color: #f8f9fa; border-left: 4px solid #dee2e6; border-radius: 4px; }}
                strong {{ color: #c62828; }}
                table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                th {{ background-color: #f8f9fa; color: #004494; }}
                .footer {{ margin-top: 50px; font-size: 0.9em; color: #777; text-align: center; border-top: 1px solid #ddd; padding-top: 20px; }}
            </style>
        </head>
        <body>
            <h1>BÁO CÁO THẨM ĐỊNH HỒ SƠ TẦN SỐ VÔ TUYẾN ĐIỆN</h1>
            
            <div style="background-color: #f8f9fa; padding: 16px; border-left: 5px solid #004494; border-radius: 4px; margin-bottom: 25px;">
                <p style="margin: 5px 0; font-size: 16px; color: #333;"><strong>👤 Chuyên viên thực hiện:</strong> {st.session_state.logged_in_user}</p>
                <p style="margin: 5px 0; font-size: 16px; color: #333;"><strong>🏢 Tên đơn vị:</strong> {ten_don_vi}</p>
                <p style="margin: 5px 0; font-size: 16px; color: #333;"><strong>⏰ Thời gian soát xét:</strong> {thoi_gian_soat}</p>
            </div>
            <hr style="border: none; border-top: 1px solid #eee; margin-bottom: 20px;">
            
            {html_body}
            <div class="footer">
                Tạo tự động bởi Hệ thống Thẩm định AI - Cục Tần số vô tuyến điện<br>
                Thời gian xuất báo cáo: {thoi_gian_soat}
            </div>
        </body>
        </html>
        """

        # --- LOGIC NÚT BẤM GỬI LÃNH ĐẠO (Đã tích hợp đồng bộ thông tin) ---
        col_gui, col_tai = st.columns([3, 7])
        
        with col_gui:
            if st.button("📤 Gửi kết quả cho LĐ", type="primary", use_container_width=True):
                if not y_kien.strip():
                    st.warning("⛔ Chưa có nội dung. Vui lòng nhập ý kiến chuyên viên soát xét!")
                else:
                    report_filename = st.session_state.get('report_filename', f"temp_{int(time.time())}.html")
                    report_path = os.path.join(REPORTS_DIR, report_filename)
                    
                    # 1. Ghi file vật lý lưu trữ cho hệ thống Admin
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(html_content)
                    
                    # 2. Ghi nhật ký lịch sử hệ thống
                    ghi_nhat_ky_he_thong(
                        can_bo=st.session_state.logged_in_user,
                        danh_sach_file=st.session_state.get('danh_sach_ten_file', []),
                        danh_sach_gp=st.session_state.get('raw_licenses', []),
                        danh_sach_to_chuc=st.session_state.get('danh_sach_to_chuc', []),
                        file_bao_cao=report_filename,
                        trang_thai="Thành công",
                        ghi_chu="Đã hoàn thành đối soát và gửi Lãnh đạo"
                    )
                    
                    # 3. Kích hoạt bắn Email thông báo cho Sếp
                    try:
                        xac_nhan_gui_lanh_dao()
                    except Exception as e:
                        pass
                    
                    st.toast("✅ Đã gửi kết quả thành công cho Lãnh đạo!", icon="🚀")
                    
        with col_tai:
            st.download_button(
                "📥 Tải bản copy về máy", 
                html_content, 
                file_name=f"Bao_cao_tham_dinh_{datetime.now(timezone(timedelta(hours=7))).strftime('%Y%m%d_%H%M')}.html",
                mime="text/html"
            )

        st.markdown("---")
        st.markdown("### 💰 Thống kê chi phí phiên làm việc")
        col_tk1, col_tk2, col_tk3, col_tk4 = st.columns(4)
        col_tk1.metric("Token đầu vào", f"{st.session_state.thong_ke_chi_phi['in_tokens']:,}")
        col_tk2.metric("Token đầu ra", f"{st.session_state.thong_ke_chi_phi['out_tokens']:,}")
        col_tk3.metric("Chi phí (USD)", f"${st.session_state.thong_ke_chi_phi['cost_usd']:.6f}")
        col_tk4.metric("Chi phí (VNĐ)", f"{st.session_state.thong_ke_chi_phi['cost_vnd']:,.0f}")

else:
    # ==========================================
    # GIAO DIỆN TRANG QUẢN TRỊ (ADMIN)
    # ==========================================
    if 'admin_authenticated' not in st.session_state:
        st.session_state.admin_authenticated = False

    col_admin, _ = st.columns([2.5, 7.5])
    with col_admin:
        if st.button("⬅️ Quay lại trang công cụ", use_container_width=True):
            st.session_state.admin_authenticated = False
            st.query_params.clear()
            st.rerun()
    
    try:
        mk_thuc_te = st.secrets.get("ADMIN_PASSWORD", None)
    except:
        st.warning("Chưa cấu hình mật khẩu Admin.")
        st.stop()

    if not st.session_state.admin_authenticated:
        with col_admin:
            mk_nhap = st.text_input("Nhập mật khẩu quản trị:", type="password")
            if mk_nhap == mk_thuc_te:
                st.session_state.admin_authenticated = True
                st.rerun()
            elif mk_nhap:
                st.error("Sai mật khẩu!")
    
    if st.session_state.admin_authenticated:
        st.success("Đăng nhập thành công!")
        
        col_title, col_refresh = st.columns([8, 2])
        with col_title:
            st.info("💡 **Hướng dẫn:** Nhấn 'Làm mới' nếu chưa thấy dữ liệu mới từ máy chủ.")
        with col_refresh:
            if st.button("🔄 Làm mới dữ liệu", use_container_width=True):
                st.cache_data.clear() 
                st.rerun()

        log_file = "nhat_ky_tham_dinh.csv"

        if os.path.exists(log_file):
            try:
                df_log = pd.read_csv(log_file, encoding='utf-8', on_bad_lines='skip')
                df_log = df_log.dropna(how='all')

                if "Trạng thái" not in df_log.columns:
                    df_log["Trạng thái"] = "Thành công (Cũ)"
                if "Ghi chú" not in df_log.columns:
                    df_log["Ghi chú"] = "-"
                
                col_chuyen_vien = "Chuyên viên thẩm định" if "Chuyên viên thẩm định" in df_log.columns else "Cán bộ thẩm định"

                # 1. ẨN CỘT SỐ GIẤY PHÉP
                if "Số Giấy phép" in df_log.columns:
                    df_log = df_log.drop(columns=["Số Giấy phép"])

                # 2. SẮP XẾP LẠI THỨ TỰ CỘT (Đưa File báo cáo xuống cuối cùng)
                cols_order = [
                    "Thời gian", 
                    col_chuyen_vien, 
                    "Tổ chức/cá nhân", 
                    "Danh sách file tải lên", 
                    "Trạng thái", 
                    "Ghi chú"
                ]
                
                # Lọc ra các cột thực sự tồn tại để tránh lỗi
                existing_cols = [c for c in cols_order if c in df_log.columns]
                
                # Chèn cột File báo cáo vào cuối cùng danh sách hiển thị
                if "File báo cáo" in df_log.columns:
                    existing_cols.append("File báo cáo")
                    
                df_log = df_log[existing_cols]

                st.subheader("📅 Nhật ký soát xét hệ thống")
                
                st.dataframe(
                    df_log.iloc[::-1], 
                    use_container_width=True, 
                    height=380,
                    column_config={
                        "Thời gian": st.column_config.TextColumn("⏰ Thời gian"),
                        col_chuyen_vien: st.column_config.TextColumn("👤 Cán bộ"),
                        "Tổ chức/cá nhân": st.column_config.TextColumn("🏢 Đơn vị"),
                        "Danh sách file tải lên": st.column_config.TextColumn("📁 File nguồn"),
                        "Trạng thái": st.column_config.TextColumn("🚦 Trạng thái"),
                        "Ghi chú": st.column_config.TextColumn("📝 Chi tiết lỗi"),
                        "File báo cáo": st.column_config.TextColumn("📄 Tên báo cáo HTML")
                    }
                )
            except Exception as e:
                st.error(f"Lỗi khi hiển thị dữ liệu nhật ký: {e}")

        st.markdown("---")
        
        col_left, col_right = st.columns([4, 6])

        with col_left:
            st.markdown("##### 📂 Chi tiết soát xét của chuyên viên")
            if os.path.exists(REPORTS_DIR):
                all_reports = sorted(
                    [f for f in os.listdir(REPORTS_DIR) if f.endswith('.html')], 
                    reverse=True
                )
                
                if not all_reports:
                    st.warning("Thư mục reports_archive đang trống.")
                    selected_hoso = None
                else:
                    with st.container(height=600, border=True):
                        selected_hoso = st.radio(
                            "Chọn file báo cáo:",
                            options=all_reports,
                            label_visibility="collapsed",
                            key="radio_admin_reports"
                        )
            else:
                st.error(f"Không tìm thấy thư mục: {REPORTS_DIR}")
                selected_hoso = None

        with col_right:
            st.markdown("##### 📄 Nội dung chi tiết")
            if selected_hoso:
                path_full = os.path.join(REPORTS_DIR, selected_hoso)
                try:
                    with open(path_full, "r", encoding="utf-8") as f:
                        html_content = f.read()
                    
                    components.html(html_content, height=800, scrolling=True)
                    
                    st.download_button(
                        "📥 Tải báo cáo về máy",
                        data=html_content,
                        file_name=selected_hoso,
                        mime="text/html"
                    )
                except Exception as e:
                    st.error(f"Lỗi đọc file: {e}")
            else:
                st.info("Vui lòng chọn một báo cáo từ danh sách bên trái.")
            
        st.markdown("---")
        st.subheader("💰 Nhật ký chi phí API")
        
        api_log_file = "api_usage_log.csv"
        if os.path.exists(api_log_file):
            df_api = pd.read_csv(api_log_file)
            st.dataframe(df_api.iloc[::-1], use_container_width=True, height=300)
            
            total_cost = df_api['Chi phí (USD)'].sum()
            st.metric("💸 Tổng chi phí hôm nay (USD)", f"${total_cost:.4f}")
            st.metric("💸 Tổng chi phí hôm nay (VNĐ)", f"{total_cost * 25400:,.0f}")
        else:
            st.info("Chưa có dữ liệu chi phí API.")