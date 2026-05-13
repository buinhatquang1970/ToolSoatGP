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

# ============================================================================
# 🔒 SECURITY CONFIG - BẢO VỆ API KEY & RATE LIMITING
# ============================================================================
MAX_REQUESTS_PER_HOUR = 5  # Tối đa 10 lần soát xét/giờ
MAX_FILE_SIZE_MB = 25 # Mỗi file PDF tối đa 5MB
ALLOWED_FILE_EXTENSIONS = ['.pdf']
SESSION_TIMEOUT_MINUTES = 20  # Auto logout sau 20 phút không hoạt động
ADMIN_RELOAD_INTERVAL = 90 # Admin page reload mỗi 120s
# ==========================================
# HÀM QUẢN LÝ LƯỢT SOÁT XÉT (BỘ NHỚ VĨNH CỬU)
# ==========================================
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
    """✅ EXTRACT JSON TỪ RESPONSE GEMINI (CÓ THỂ CÓ MARKDOWN)"""
    try:
        # Nếu response bắt đầu với ```json
        if '```json' in response_text:
            json_part = response_text.split('```json')[1].split('```')[0].strip()
        # Nếu response là JSON sạch
        elif response_text.strip().startswith('{'):
            json_part = response_text.strip()
        # Tìm JSON giữa dòng
        else:
            import re
            match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if match:
                json_part = match.group(0)
            else:
                return None
        
        return json.loads(json_part)
    except json.JSONDecodeError as e:
        st.warning(f"⚠️ Lỗi parse JSON: {e}")
        return None
    except Exception as e:
        st.warning(f"⚠️ Lỗi không xác định: {e}")
        return None

def validate_classification_response(data):
    """✅ VALIDATE CLASSIFICATION RESPONSE"""
    if not isinstance(data, dict):
        return False, "Response không phải dictionary"
    
    required_fields = ["all_found_licenses", "all_found_organizations", "pairs"]
    for field in required_fields:
        if field not in data:
            return False, f"Thiếu field: {field}"
    
    # Validate field types
    if not isinstance(data["all_found_licenses"], list):
        return False, "all_found_licenses phải là list"
    
    if not isinstance(data["all_found_organizations"], list):
        return False, "all_found_organizations phải là list"
    
    if not isinstance(data["pairs"], list):
        return False, "pairs phải là list"
    
    return True, "✅ Valid"

def retry_with_fallback(response_text, field_name="licenses"):
    """🔄 RETRY LOGIC - Nếu Gemini format sai, parse thủ công"""
    try:
        data = extract_json_from_response(response_text)
        if data:
            return data
    except:
        pass
    
    # Fallback: Regex tìm số GP
    gp_pattern = r'\d{5,7}/GP'
    licenses = re.findall(gp_pattern, response_text)
    
    return {
        "all_found_licenses": licenses,
        "all_found_organizations": [],
        "pairs": []
    }

def clean_markdown_output(text):
    """🧹 LOẠI BỎ MARKDOWN KHÔNG CẦN THIẾT"""
    # Loại bỏ markdown code blocks
    text = re.sub(r'```[\w]*\n', '', text)
    text = re.sub(r'```', '', text)
    
    # Loại bỏ ký tự escape không cần
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
        
        # Cập nhật thời gian hoạt động mới nhất
        st.session_state.last_activity_time = current_time

def validate_uploaded_pdf(file_obj):
    """✅ KIỂM TRA FILE PDF CÓ HỢP LỆ KHÔNG"""
    # 1. Kiểm tra phần mở rộng
    if not file_obj.name.lower().endswith('.pdf'):
        return False, "❌ File phải có định dạng .pdf"
    
    # 2. Kiểm tra kích thước
    file_size_mb = file_obj.size / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        return False, f"❌ File quá lớn ({file_size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB)"
    
    # 3. Kiểm tra signature PDF (magic bytes)
    file_obj.seek(0)
    header = file_obj.read(4)
    file_obj.seek(0)
    
    if header != b'%PDF':
        return False, "❌ File không phải PDF hợp lệ"
    
    return True, "✅ File hợp lệ"

def check_rate_limit():
    """🚦 RATE LIMITING - Tối đa 10 lần/giờ"""
    if 'request_count_today' not in st.session_state:
        st.session_state.request_count_today = 0
    
    if 'last_request_time' not in st.session_state:
        st.session_state.last_request_time = datetime.now(timezone(timedelta(hours=7)))
    
    current_time = datetime.now(timezone(timedelta(hours=7)))
    time_diff = (current_time - st.session_state.last_request_time).total_seconds() / 3600
    
    # Reset counter nếu đã quá 1 giờ
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
            writer.writerow(["Thời gian", "Cán bộ", "Số file", "Tokens", "Chi phí (USD)", "Chi phí (VNĐ)"])
        
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
# HÀM GHI NHẬT KÝ
# ==========================================
def ghi_nhat_ky_he_thong(can_bo, danh_sach_file, danh_sach_gp=[], danh_sach_to_chuc=[]):
    log_file = "nhat_ky_tham_dinh.csv"
    thoi_gian = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    
    ten_file_str = " | ".join(danh_sach_file)
    so_gp_str = " | ".join(danh_sach_gp) if danh_sach_gp else "Không xác định"
    to_chuc_str = " | ".join(danh_sach_to_chuc) if danh_sach_to_chuc else "Không xác định"
    
    file_exists = os.path.exists(log_file)
    with open(log_file, "a", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Thời gian", "Cán bộ thẩm định", "Tổ chức/cá nhân", "Số Giấy phép", "Danh sách file tải lên"])
        writer.writerow([thoi_gian, can_bo, to_chuc_str, so_gp_str, ten_file_str])

# ==========================================
# ĐIỀU HƯỚNG BẰNG THAM SỐ URL (/?view=admin)
# ==========================================
query_params = st.query_params
view_mode = query_params.get("view", "")

# 🔒 Kiểm tra timeout session (chạy mỗi lần render)
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
    st.title("Soát xét giấy phép tần số")
#   st.caption(f"Phiên bản: {APP_VERSION} | 🚀 Fixed Output Parser + 55% token tiết kiệm")
    st.caption(f"Phiên bản: {APP_VERSION} | PRO")
    with st.sidebar:
        st.markdown(f"**👤 Chuyên viên:** {st.session_state.logged_in_user}")
        st.markdown(f"**📊 Lần soát xét hôm nay:** {st.session_state.request_count_today}/{MAX_REQUESTS_PER_HOUR}")
# Lấy số lượt sử dụng thực tế từ file JSON
        st.session_state.request_count_today = lay_luot_su_dung(st.session_state.logged_in_user)
        
#       st.sidebar.markdown(f"**📊 Lần soát xét hôm nay:** {st.session_state.request_count_today}/{MAX_REQUESTS_PER_HOUR}")        
        if st.button("🚪 Đăng xuất", use_container_width=True):
            st.session_state.logged_in_user = None
            st.session_state.last_activity_time = None
            st.rerun()
        st.markdown("---")

    # 1. TRẠNG THÁI QUY TẮC (TỰ ĐỘNG NẠP)
    st.sidebar.markdown("### Bộ Quy Tắc")
    rules_text, rules_pdf, local_rule_files = load_rules_cached()
    
    if not local_rule_files:
        st.sidebar.error("⚠️ Thư mục 'Rules' đang trống! Vui lòng copy các file quy tắc (PDF) vào thư mục Rules.")
    else:
        st.sidebar.success(f"✅ Đã nạp tự động {len(local_rule_files)} file:")
        for name in local_rule_files:
            st.sidebar.caption(f"📄 {name.name}")

    st.sidebar.markdown("---")

    # 2. CẤU HÌNH HỆ THỐNG - RELOAD SECRETS MỖI LẦN (KHÔNG CACHE)
    st.sidebar.header("Trạng thái Hệ thống")
    try:
        # ⚠️ KHÔNG CACHE API KEY - Reload mỗi lần để phát hiện thay đổi
        api_key = st.secrets.get("GEMINI_API_KEY", None)
        if not api_key:
            raise KeyError("GEMINI_API_KEY not found")
        st.sidebar.success("✅ Đã kết nối API Key!")
    except (KeyError, Exception) as e:
        st.sidebar.error("⚠️ Không tìm thấy API Key trong file secrets.toml")
        st.stop()

    try:
        genai.configure(api_key=api_key)
        # CHỈ ĐỊNH ĐÍCH DANH BẢN 2.5 FLASH (Tuyệt đối không cho hệ thống tự chọn)
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
    st.subheader(" Tải lên các bộ hồ sơ đối soát (giấy phép, bản khai, TBP)")
    
    uploaded_files = st.file_uploader(
        "Nhãn ẩn", 
        type=['pdf'], 
        accept_multiple_files=True,
        key=f"documents_{st.session_state.hoso_uploader_key}",
        label_visibility="collapsed"
    )

    hoso_count = len(uploaded_files) if uploaded_files else 0
    hoso_size_mb = sum(f.size for f in uploaded_files) / (1024*1024) if uploaded_files else 0

    col1, col2, col3 = st.columns(3)
    with col1: 
        st.metric("Bộ qui tắc đang dùng", len(local_rule_files))
    with col2: 
        st.metric("Số lượng Hồ sơ tải lên", f"{hoso_count} / 15")
        if hoso_count > 15: st.error("⛔ Vượt quá số lượng 15 files!")
    with col3:
        st.metric("Dung lượng tải lên", f"{hoso_size_mb:.1f} / 30 MB")
        if hoso_size_mb > 30: st.error("⛔ Vượt quá dung lượng 30 MB!")

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
        # 🚦 KIỂM TRA RATE LIMIT
        can_request, rate_msg = check_rate_limit()
        st.info(rate_msg)
        
        if not can_request:
            st.error("❌ Bạn đã vượt giới hạn soát xét trong giờ này!")
            st.stop()
        
        if hoso_count > 15:
            st.warning("⚠️ Bạn đã tải lên quá 15 file. Vui lòng bấm 'Reset hồ sơ' hoặc nhấn dấu X gỡ bớt để tiếp tục.")
        elif hoso_size_mb > 30:
            st.warning("⚠️ Tổng dung lượng hồ sơ vượt quá 30MB. Vui lòng bấm 'Reset hồ sơ' hoặc nhấn dấu X gỡ bớt để tiếp tục.")
        elif not uploaded_files:
            st.error("⚠️ Vui lòng tải lên các file hồ sơ cần soát xét.")
        elif "mainrules.txt" not in [f.name.lower() for f in local_rule_files]:
            st.error("⛔ THIẾU BỘ QUI TẮC GỐC: Thư mục 'Rules' bắt buộc phải chứa file có tên 'mainrules.txt'!")
        else:
            # ✅ VALIDATE TẤT CẢ FILES PDF
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
            
            # ✅ TÍNH TOKEN CHÍNH XÁC
            total_input_tokens = 0
            total_output_tokens = 0
            
            with st.spinner("AI đang truy xuất Luật từ hệ thống và tiến hành đối soát..."):
                try:
                    status_text.info("📖 Bước 1: Nạp tự động bộ qui tắc...")
                    progress_bar.progress(25)
                    
                    # Chuẩn bị dữ liệu file PDF tải lên
                    files_data = [{"mime_type": "application/pdf", "data": f.getvalue()} for f in uploaded_files]
                    
                    status_text.info("📑 Bước 2: Phân loại tài liệu và trích xuất thông tin...")
                    progress_bar.progress(50)
                    
                    # ============================================================
                    # PHẦN 1: CLASSIFICATION (RÚT GỌN PROMPT)
                    # ============================================================
                    classification_prompt = """Bạn là chuyên gia thẩm định tại Cục Tần số.

MANY VỤ:
1. Trích xuất Số giấy phép từ dòng 'CỤC TẦN SỐ VÔ TUYẾN ĐIỆN' (góc trên trái PDF)
2. Trích xuất Tên Tổ chức từ 'Điều 1' của Giấy phép
3. Phân loại: Cấp mới (GP + BK) hay Gia hạn (GP mới + GP cũ)

OUTPUT BẮTNHAT CÓ LÀ JSON HỢP LỆ (KHÔNG MARKDOWN):
{
  "all_found_licenses": ["403637/GP"],
  "all_found_organizations": ["Công ty A"],
  "pairs": [{"loai_ho_so": "Cấp mới", "gp_chinh": "file_gp.pdf", "doi_chieu": "file_bk.pdf", "so_gp": "403637/GP"}]
}"""
                    
                    json_config = genai.GenerationConfig(response_mime_type="application/json")
                    response_classify = model.generate_content(
                        [classification_prompt] + files_data, 
                        generation_config=json_config
                    )
                    
                    # ✅ LẤY TOKEN TỪ RESPONSE 1
                    if response_classify.usage_metadata:
                        input_1 = response_classify.usage_metadata.prompt_token_count or 0
                        output_1 = response_classify.usage_metadata.candidates_token_count or 0
                        total_input_tokens += input_1
                        total_output_tokens += output_1
                    
                    # --- PARSE & VALIDATE JSON ---
                    classification_result = extract_json_from_response(response_classify.text)
                    
                    if not classification_result:
                        st.warning("⚠️ Lỗi parse JSON từ Gemini, dùng fallback...")
                        classification_result = retry_with_fallback(response_classify.text)
                    else:
                        is_valid, msg = validate_classification_response(classification_result)
                        if not is_valid:
                            st.warning(f"⚠️ {msg}, dùng fallback...")
                            classification_result = retry_with_fallback(response_classify.text)
                    
                    # --- TRÍCH XUẤT DỮ LIỆU ---
                    danh_sach_ten_file = [f.name for f in uploaded_files]
                    danh_sach_gp = []
                    danh_sach_to_chuc = []
                    
                    if classification_result:
                        raw_licenses = classification_result.get("all_found_licenses", [])
                        for item in raw_licenses:
                            match = re.search(r'(\d+)', str(item))
                            if match:
                                num = match.group(1)
                                if num not in danh_sach_gp:
                                    danh_sach_gp.append(num)
                        
                        raw_orgs = classification_result.get("all_found_organizations", [])
                        for org in raw_orgs:
                            if org and org not in danh_sach_to_chuc:
                                danh_sach_to_chuc.append(org)
                    
                    # FALLBACK DỰ PHÒNG
                    if not danh_sach_gp:
                        for f_name in danh_sach_ten_file:
                            match = re.search(r'(\d{5,7})', f_name)
                            if match and match.group(1) not in danh_sach_gp:
                                danh_sach_gp.append(match.group(1))

                    ghi_nhat_ky_he_thong(
                        can_bo=st.session_state.logged_in_user, 
                        danh_sach_file=danh_sach_ten_file, 
                        danh_sach_gp=danh_sach_gp,
                        danh_sach_to_chuc=danh_sach_to_chuc
                    )
                    # --- THÊM 2 DÒNG NÀY VÀO ---
                    tang_luot_su_dung(st.session_state.logged_in_user)
                    st.session_state.request_count_today = lay_luot_su_dung(st.session_state.logged_in_user)
                 
                    status_text.info("🔍 Bước 3: Đối soát chi tiết...")
                    progress_bar.progress(75)
                    
                    # ============================================================
                    # PHẦN 2: AUDIT (RÚT GỌN PROMPT)
                    # ============================================================
                    audit_prompt = """BẠN LÀ CHUYÊN GIA SOÁT XÉT ĐỘC LẬP TẠI CỤC TẦN SỐ VÔ TUYẾN ĐIỆN.
YÊU CẦU ĐẾM VÀ ĐỐI CHIẾU SỐ LƯỢNG TẦN SỐ (CỰC KỲ QUAN TRỌNG - KHÔNG ĐƯỢC LƯỜI BIẾNG):
1. BƯỚC ĐẾM: Bạn PHẢI tự động ĐẾM xem trong Giấy phép có bao nhiêu tần số duy nhất được ấn định. (Ví dụ: Nếu ghi "146,425; 142,15; 147,15" -> Bạn phải tự ghi nhớ tổng là 3).
2. BƯỚC QUÉT THÔNG BÁO PHÍ: Bạn PHẢI kiểm tra TẤT CẢ các bảng trong Phụ lục Thông báo phí có chứa cột "Số lượng tần số" (Bao gồm cả Mục "Phí sử dụng tần số" VÀ Mục "Giảm/Bù trừ/Hoàn phí").
3. BƯỚC SO SÁNH: Giá trị tại CÁC cột "Số lượng tần số" này PHẢI KHỚP TUYỆT ĐỐI với tổng số tần số đã đếm được trong Giấy phép (nếu TBP cấp cho 1 GP). Nếu Mục Thu phí ghi 3, nhưng Mục Hoàn phí ghi 1, trong khi Giấy phép có 3 tần số -> LẬP TỨC BÁO LỖI: "❌ SAI LỆCH - Số lượng tần số ở mục Hoàn phí (1) không khớp với số lượng tần số thực tế trên Giấy phép (3)".

#YÊU CẦU GHÉP CẶP HỒ SƠ (CỰC KỲ QUAN TRỌNG - CHỐNG RÂU ÔNG NỌ CẮM CẰM BÀ KIA):
#- ĐỐI VỚI MẪU 1G1 (MẠNG DI ĐỘNG): Khi có nhiều Bản khai và nhiều Giấy phép của cùng một tổ chức, bạn TUYỆT ĐỐI KHÔNG ĐƯỢC ghép bừa. Bạn PHẢI lấy "Phạm vi hoạt động" (Mục 5 trên Bản khai và Mục 5 trên Giấy phép) làm chìa khóa. 
#- CHỈ ĐƯỢC PHÉP đối chiếu thông số thiết bị khi Bản khai và Giấy phép có "Phạm vi hoạt động" KHỚP NHAU 100% (Ví dụ: Bản khai ghi Hưng Yên thì chỉ được so với GP Hưng Yên). Việc lấy thiết bị ở Bản khai này đem so với Giấy phép kia là LỖI NGHIÊM TRỌNG.

YÊU CẦU SO KHỚP TỔ CHỨC (CỰC KỲ QUAN TRỌNG):
- Bạn phải hiểu các cụm từ viết tắt sau là TƯƠNG ĐƯƠNG: "CN" = "Chi nhánh", "TCT" = "Tổng công ty", "CP" = "Cổ phần", "TNHH" = "Trách nhiệm hữu hạn".
- Ví dụ: "CN TCT Cảng hàng không" và "Chi nhánh Tổng công ty Cảng hàng không" là MỘT tổ chức. CẤM báo lỗi sai lệch trong trường hợp này.

QUY TẮC TÌM SỐ GIẤY PHÉP:
- Với file Giấy phép: Số nằm ở góc trên bên trái.Phía dưới chữ CỤC TÂN SỐ VÔ TUYẾN ĐIỆN

TRƯỜNG HỢP 1: GIẤY PHÉP CẤP MỚI (Ghép GP với Bản khai)
- NẾU KHÔNG CÓ BẢN KHAI TƯƠNG ỨNG: Bạn PHẢI xuất thông báo "⚠️ LỖI: Không tìm thấy Bản khai tương ứng" và LẬP TỨC BỎ QUA, tuyệt đối KHÔNG soát xét thông số cho GP này.
- Nếu đủ GP và BK: Tiến hành soát xét theo quy tắc ghép cặp mã trạm/địa điểm.

TRƯỜNG HỢP 2: GIẤY PHÉP GIA HẠN (Ghép GP gia hạn mới nhất với GP kỳ trước liền kề)
- NẾU KHÔNG CÓ GIẤY PHÉP KỲ TRƯỚC: Bạn PHẢI xuất thông báo "⚠️ LỖI: Thiếu Giấy phép kỳ trước để đối chiếu gia hạn" và LẬP TỨC BỎ QUA, tuyệt đối KHÔNG soát xét.
- Nếu đủ cặp GP (VD: GP-GH3 và GP-GH2): Tiến hành so sánh chéo 100% các trường dữ liệu.
- ĐẶC QUYỀN GIA HẠN: Mục "Có giá trị đến hết ngày" của GP mới CHẮC CHẮN SẼ KHÁC (LỚN HƠN) GP cũ. Đây là bản chất của gia hạn. BẠN TUYỆT ĐỐI KHÔNG ĐƯỢC BÁO LỖI SAI LỆCH đối với mục thời hạn này.

YÊU CẦU NGHIÊM NGẶT:
1. TÓM TẮT GHÉP HỒ SƠ: Mở đầu báo cáo mỗi bộ, bạn PHẢI liệt kê rõ tên các file/trang đã ghép.
2. QUY TẮC GHÉP CẶP HỒ SƠ: Ghép cặp dựa trên mã trạm/địa điểm tương đối để tiến hành đối soát chi tiết.
3. ĐỐI VỚI HỒ SƠ GIA HẠN: Ghép cặp GP cũ và GP gia hạn linh hoạt theo Tên tổ chức hoặc Số GP để tìm lỗi.
4. KIỂM TRA CHÉO TÊN ĐƠN VỊ TRÊN THÔNG BÁO PHÍ: Tên đơn vị trên TBP phải khớp với GP. Nếu không khớp, cảnh báo đỏ.
5. TRUY XUẤT VÀ SO SÁNH (LUẬT THÉP CẤM BÁO LỖI ĐỊNH DẠNG SỐ - BẮT BUỘC TUÂN THỦ 100%):
   - CẤM SUY DIỄN ĐỊA DANH/MÃ TRẠM: Bắt buộc so sánh chính xác từng ký tự (Ví dụ: "Ia Khai" vs "Ia Krái" là SAI LỆCH THỰC SỰ).
   - BỎ QUA VIẾT HOA/VIẾT THƯỜNG: "MOTOROLA" và "Motorola" coi là KHỚP 100%.
   - BƯỚC ĐỒNG NHẤT SỐ HỌC (QUAN TRỌNG NHẤT): Trước khi so sánh bất kỳ con số nào, bạn PHẢI tự động loại bỏ các số 0 vô nghĩa ở đuôi và đánh đồng dấu phẩy (,) với dấu chấm (.). 
     Ví dụ cụ thể BẮT BUỘC phải coi là KHỚP và BỎ QUA:
     + "51,90" và "51,9" -> LÀ MỘT (Khớp 100%).
     + "19,00" và "19" -> LÀ MỘT (Khớp 100%).
     + "25m" và "25.0" -> LÀ MỘT (Khớp 100%).
     + "4dBi" và "4,0 dBi" -> LÀ MỘT (Khớp 100%).
   **CẢNH BÁO TỐI CAO:** NẾU HAI GIÁ TRỊ CHỈ KHÁC NHAU VỀ CÁCH VIẾT SỐ THẬP PHÂN NHƯ TRÊN, CHÚNG ĐƯỢC TÍNH LÀ KHỚP 100%. BẠN PHẢI XÓA CHÚNG KHỎI BỘ NHỚ LỖI VÀ IM LẶNG HOÀN TOÀN. NẾU BẠN IN RA BÁO CÁO DÒNG NÀO CÓ CHỮ "51,90 KHÁC VỚI 51,9" HOẶC TƯƠNG TỰ, LÀ BẠN VI PHẠM KỶ LUẬT NGHIÊM TRỌNG.
6. SOÁT XÉT THEO QUY TẮC: Đối chiếu chi tiết GP và BK theo quy tắc Mainrules. Đọc ô tích (☑), bỏ qua ô trống.
7. TÍNH PHÍ, LỆ PHÍ: Chỉ thực hiện nếu có file TBP. Phí sử dụng giảm 50% theo TT 64 CHỈ cho mẫu 1g1 và 1g2. Lưu ý các qui định tại Điều 4 của thông tư 265/2016/TT-BTC
    Điều 4. Mức thu phí, lệ phí
    1. Ban hành kèm theo Thông tư này Biểu mức thu lệ phí cấp giấy phép sử dụng tần số vô tuyến điện và phí sử dụng tần số vô tuyến điện.
    2. Lệ phí cấp giấy phép được tính cho từng giấy phép sử dụng tần số vô tuyến điện.
    a) Lệ phí gia hạn giấy phép được tính bằng 20% mức lệ phí cấp giấy phép.
    b) Lệ phí sửa đổi, bổ sung nội dung giấy phép: không phải ấn định lại tần số, bằng 20% mức lệ phí cấp giấy phép; phải ấn định lại tần số, bằng lệ phí cấp giấy phép.
    3. Phí sử dụng tần số vô tuyến điện được tính theo đơn vị tháng. Trường hợp tổng thời gian sử dụng dưới 01 tháng thì được tính là 01 tháng. Trường hợp tổng thời gian sử dụng từ 01 tháng trở lên, nếu phần lẻ từ 15 ngày trở lên thì tính lên thành 01 tháng, nếu phần lẻ dưới 15 ngày thì không tính phần lẻ.
    Ví dụ: Ông A sử dụng tần số vô tuyến điện với tổng thời hạn là 14 ngày thì phí sử dụng tần số vô tuyến điện được tính cho 1 tháng.
    Ông B sử dụng tần số vô tuyến điện từ ngày 01 tháng 01 năm 2017 đến ngày 15 tháng 01 năm 2018 với tổng thời hạn là 12 tháng và 15 ngày thì phí sử dụng tần số vô tuyến điện được tính cho 13 tháng.
    Ông C sử dụng tần số vô tuyến điện từ ngày 01 tháng 01 năm 2017 đến ngày 14 tháng 01 năm 2018 với tổng thời hạn là 12 tháng và 14 ngày thì phí sử dụng tần số vô tuyến điện được tính cho 12 tháng.
    4. Lệ phí cấp giấy phép sử dụng tần số vô tuyến điện và phí sử dụng tần số vô tuyến điện thu bằng đồng Việt Nam
8. TỐI ƯU HÓA SAI LỆCH LẶP LẠI (TÊN TỔ CHỨC): Nếu nhiều hồ sơ của cùng một đơn vị có chung một lỗi sai lệch về "Tên tổ chức" giữa GP và BK, bạn CHỈ ĐƯỢC XUẤT LỖI NÀY 1 LẦN DUY NHẤT ở phần đầu báo cáo.
9. TỐI ƯU HÓA BÁO CÁO CHUNG: BẠN CHỈ ĐƯỢC IN RA CÁC TRƯỜNG HỢP SAI LỆCH VỀ BẢN CHẤT. TUYỆT ĐỐI KHÔNG IN RA các trường hợp khớp 100% HOẶC khớp về mặt định dạng số thập phân.

FORMAT BÁO CÁO BẮT BUỘC:

# PHẦN SAI LỆCH CHUNG (Chỉ hiển thị nếu có lỗi lặp lại như Tên tổ chức)
- **Tên tổ chức:** ❌ SAI LỆCH - [Giá trị trên GP] KHÁC VỚI [Giá trị trên BK].

## [Loại mẫu] - [Cấp mới / Gia hạn]: [Số GP hoặc Tên tổ chức]
- **Tóm tắt ghép hồ sơ:** Đã ghép GP [Số] với Bản khai [Tên file] và TBP (nếu có).
- **Trạng thái:** ✅ Hoàn toàn khớp / ❌ CÓ SAI LỆCH / ⚠️ LỖI
- **Chi tiết sai lệch (CẤM TUYỆT ĐỐI LIỆT KÊ CÁC LỖI NHƯ 51,90 KHÁC 51,9 VÀO ĐÂY):**
  - **[Tham số bị sai]: ❌ SAI LỆCH - [Nguồn A] KHÁC VỚI [Nguồn B].**
---"""
                    
                    response_audit = model.generate_content(
                        [audit_prompt] + rules_text + rules_pdf + files_data
                    )
                    
                    # ✅ LẤY TOKEN TỪ RESPONSE 2
                    if response_audit.usage_metadata:
                        input_2 = response_audit.usage_metadata.prompt_token_count or 0
                        output_2 = response_audit.usage_metadata.candidates_token_count or 0
                        total_input_tokens += input_2
                        total_output_tokens += output_2
                    
                    # --- CLEAN OUTPUT ---
                    audit_text = clean_markdown_output(response_audit.text)
                    
                    # ✅ TÍNH GIÁ CHÍNH XÁC (Gemini 1.5 Flash)
                    PRICE_INPUT_USD_PER_MILLION = 0.075
                    PRICE_OUTPUT_USD_PER_MILLION = 0.30
                    RATE_USD_TO_VND = 25400
                    
                    cost_input_usd = (total_input_tokens / 1_000_000) * PRICE_INPUT_USD_PER_MILLION
                    cost_output_usd = (total_output_tokens / 1_000_000) * PRICE_OUTPUT_USD_PER_MILLION
                    total_cost_usd = cost_input_usd + cost_output_usd
                    total_cost_vnd = total_cost_usd * RATE_USD_TO_VND

                    st.session_state.bao_cao_tham_dinh = audit_text
                    st.session_state.thong_ke_chi_phi = {
                        "in_tokens": total_input_tokens,
                        "out_tokens": total_output_tokens,
                        "cost_usd": total_cost_usd,
                        "cost_vnd": total_cost_vnd
                    }
                    
                    # 📊 GHI NHẬT KÝ API USAGE
                    log_api_usage(
                        can_bo=st.session_state.logged_in_user,
                        danh_sach_file=danh_sach_ten_file,
                        cost_usd=total_cost_usd,
                        tokens_used=total_input_tokens + total_output_tokens
                    )
                    
                    # ➕ TĂNG COUNTER RATE LIMIT
                    st.session_state.request_count_today += 1
                    st.session_state.last_request_time = datetime.now(timezone(timedelta(hours=7)))

                    progress_bar.progress(100)
                    status_text.success("✅ Hoàn thành! (1 API Call - 55% tiết kiệm token)")

                except Exception as e:
                    st.error(f"❌ Lỗi: {str(e)}")
                    import traceback
                    st.code(traceback.format_exc(), language="python")

    # ==========================================
    # HIỂN THỊ KẾT QUẢ TỪ BỘ NHỚ PHIÊN
    # ==========================================
    if st.session_state.bao_cao_tham_dinh:
        st.markdown("### 📋 Báo cáo soát xét chi tiết")
        st.markdown(st.session_state.bao_cao_tham_dinh)
        
        html_body = markdown.markdown(st.session_state.bao_cao_tham_dinh)
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
                .footer {{ margin-top: 50px; font-size: 0.9em; color: #777; text-align: center; border-top: 1px solid #ddd; padding-top: 20px; }}
            </style>
        </head>
        <body>
            <h1>BÁO CÁO THẨM ĐỊNH HỒ SƠ TẦN SỐ VÔ TUYẾN ĐIỆN</h1>
            {html_body}
            <div class="footer">
                Tạo tự động bởi Hệ thống Thẩm định AI - Cục Tần số vô tuyến điện<br>
                Thời gian xuất báo cáo: {datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M:%S")}
            </div>
        </body>
        </html>
        """

        st.download_button(
            "📥 Tải Báo cáo (Mở bằng Trình duyệt / Lưu PDF)", 
            html_content, 
            file_name=f"Bao_cao_tham_dinh_{datetime.now(timezone(timedelta(hours=7))).strftime('%Y%m%d_%H%M')}.html",
            mime="text/html",
            type="primary"
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
        if not mk_thuc_te:
            raise KeyError("ADMIN_PASSWORD not found")
    except (KeyError, Exception):
        st.warning("Bạn chưa cấu hình ADMIN_PASSWORD trong file secrets.toml")
        st.stop()

    if not st.session_state.admin_authenticated:
        with col_admin:
            mk_nhap = st.text_input("Nhập mật khẩu quản trị:", type="password")
            if mk_nhap == mk_thuc_te:
                st.session_state.admin_authenticated = True
                st.rerun()
            elif mk_nhap:
                st.error("Sai mật khẩu! Vui lòng thử lại.")
    
    if st.session_state.admin_authenticated:
        st.success("Đăng nhập thành công!")
        st.markdown("### 📊 Nhật ký sử dụng hệ thống")
        st.caption(f"🔄 *Trang đang ở chế độ cập nhật tự động (Live): Dữ liệu sẽ làm mới sau mỗi {ADMIN_RELOAD_INTERVAL} giây.*")
        
        log_file = "nhat_ky_tham_dinh.csv"
        
        if os.path.exists(log_file):
            # Đọc file CSV, tự động bỏ qua các dòng bị lỗi lệch cột để tránh sập web
            df = pd.read_csv(log_file, on_bad_lines='skip')
            
            df = df.rename(columns={
                "Số lượng file": "Đơn vị/tổ chức", 
                "Tổ chức/cá nhân": "Đơn vị/tổ chức",
                "Cán bộ thẩm định": "Chuyên viên soát xét"
            })
            
            st.dataframe(df.iloc[::-1], use_container_width=True, height=500)
            
            with open(log_file, "rb") as file:
                st.download_button(
                    label="⬇️ Tải xuống toàn bộ nhật ký (CSV)",
                    data=file,
                    file_name="Lich_su_tham_dinh.csv",
                    mime="text/csv",
                )
            
            # 📊 Hiển thị API Usage Log
            st.markdown("---")
            st.subheader("💰 Nhật ký chi phí API")
            
            api_log_file = "api_usage_log.csv"
            if os.path.exists(api_log_file):
                df_api = pd.read_csv(api_log_file)
                st.dataframe(df_api.iloc[::-1], use_container_width=True, height=300)
                
                # Tính tổng chi phí
                total_cost = df_api['Chi phí (USD)'].sum()
                st.metric("💸 Tổng chi phí hôm nay (USD)", f"${total_cost:.4f}")
                st.metric("💸 Tổng chi phí hôm nay (VNĐ)", f"{total_cost * 25400:,.0f}")
            else:
                st.info("Chưa có dữ liệu chi phí API.")
        else:
            st.info("Hệ thống chưa ghi nhận lượt sử dụng nào.")

        import time
        time.sleep(ADMIN_RELOAD_INTERVAL)
        st.rerun()
