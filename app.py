import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os
import platform
import json
import requests
import re
import time
from pathlib import Path
try:
    from rapidfuzz import fuzz as rfuzz
except ImportError:
    rfuzz = None
from calendar import monthrange

# --- [1. 시스템 초기 설정] ---
# 공개 배포 모드: 환경변수 REVA_PUBLIC=true 시 읽기 전용 4개 메뉴만 노출
try:
    _pub_secret = st.secrets.get("REVA_PUBLIC", "")
except Exception:
    _pub_secret = ""
PUBLIC_MODE = os.environ.get("REVA_PUBLIC", str(_pub_secret)).lower() in ("1", "true", "yes")

_BASE_DIR = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if PUBLIC_MODE
    else os.path.dirname(os.path.abspath(__file__))
)

def _icloud_base() -> Path:
    """macOS / Windows iCloud Drive 경로를 자동 감지."""
    if platform.system() == "Darwin":
        return Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / "iCloudDrive",
        Path(os.environ.get("USERPROFILE", "")) / "Apple/Mobile Documents/com~apple~CloudDocs",
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path(os.environ.get("USERPROFILE", "")) / "iCloudDrive"

ICLOUD_BASE = _icloud_base()

DB_FILE           = os.path.join(_BASE_DIR, "broadcast_log.csv")
DETAILS_FILE      = os.path.join(_BASE_DIR, "broadcast_details.json")
CONFIG_FILE       = os.path.join(_BASE_DIR, "config.json")
IMAGE_SAVE_DIR    = os.path.join(_BASE_DIR, "방송_완성작_아카이브")
BACKUP_DIR        = os.path.join(_BASE_DIR, "backups")
CHANNEL_ID        = "6ab86891e07489743437594c6e4dbf3a"
LIVE_INFO_DIR     = str(ICLOUD_BASE / "RevaBroadcast" / "방송정보")
TIMELINE_SAVE_DIR = os.path.join(_BASE_DIR, "타임라인")
SOURCE_SAVE_DIR   = os.path.join(_BASE_DIR, "소스")
CLIP_SAVE_DIR     = os.path.join(_BASE_DIR, "레바짤")
CLIP_DB_FILE      = os.path.join(_BASE_DIR, "clip_log.csv")
REGISTRY_FILE     = os.path.join(TIMELINE_SAVE_DIR, "_registry.json")
NORMALIZE_LOG     = os.path.join(_BASE_DIR, "_normalize_log.jsonl")

for _d in [TIMELINE_SAVE_DIR, SOURCE_SAVE_DIR, CLIP_SAVE_DIR]:
    os.makedirs(_d, exist_ok=True)

CSV_HEADER = ["완료", "제목", "파일명", "날짜", "시작시간", "방송길이", "주제", "part", "상세내용", "메모", "URL", "손님", "영도", "주식", "이미지파일명", "카테고리", "고유 라이브 ID", "금일 방송 횟수", "타임라인CSV", "소스CSV"]

ABS_DIR = os.path.abspath(IMAGE_SAVE_DIR)
if not os.path.exists(ABS_DIR):
    os.makedirs(ABS_DIR, exist_ok=True)

# --- [자동 감지 및 레바실록 등록 기능] ---
def auto_sync_live_info():
    if not os.path.exists(LIVE_INFO_DIR): return

    # DB 파일 컬럼 동기화
    if os.path.exists(DB_FILE):
        try:
            df = pd.read_csv(DB_FILE, dtype=str).fillna("")
            changed = False
            for col in ["카테고리", "고유 라이브 ID", "금일 방송 횟수", "타임라인CSV", "소스CSV"]:
                if col not in df.columns:
                    df[col] = ""
                    changed = True
            if changed:
                df[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
        except: pass

    existing_ids = []
    if os.path.exists(DB_FILE):
        try:
            df = pd.read_csv(DB_FILE, dtype=str).fillna("")
            if "고유 라이브 ID" in df.columns:
                existing_ids = df["고유 라이브 ID"].tolist()
        except: pass

    json_files = [f for f in os.listdir(LIVE_INFO_DIR) if f.endswith('.json')]

    # 1단계: 모든 json 파싱 후 날짜별 그룹화
    file_data_cache = {}
    date_groups = {}  # ds → [(start_t, l_id, filename), ...]

    for filename in json_files:
        file_path = os.path.join(LIVE_INFO_DIR, filename)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            l_id = str(data.get('live_id', data.get('lived_id', ''))).strip()
            if not l_id:
                continue
            start_time_full = str(data.get('start_time', ''))
            if ' ' in start_time_full:
                raw_date = start_time_full.split()[0]
                start_t  = start_time_full.split()[1][:8]
            elif 'T' in start_time_full:
                raw_date = start_time_full.split('T')[0]
                start_t  = start_time_full.split('T')[1][:8]
            else:
                raw_date = datetime.now().strftime('%Y-%m-%d')
                start_t  = start_time_full[:8] if start_time_full else ""
            ds = raw_date.replace('-', '')
            date_groups.setdefault(ds, []).append((start_t, l_id, filename))
            file_data_cache[filename] = (data, ds, start_t, l_id)
        except:
            continue

    # 2단계: 날짜별 시작시간 오름차순 정렬 → part 번호 결정
    date_part_map = {}  # (ds, l_id) → part_num (0=단독방송, 1/2/...=다부제)
    for ds, entries in date_groups.items():
        entries_sorted = sorted(entries, key=lambda x: x[0])
        for i, (start_t, l_id, fname) in enumerate(entries_sorted):
            date_part_map[(ds, l_id)] = (i + 1) if len(entries_sorted) >= 2 else 0

    # 3단계: DB에 없는 항목만 등록
    for filename, (data, ds, start_t, l_id) in file_data_cache.items():
        if l_id in existing_ids:
            continue
        try:
            part_num = date_part_map.get((ds, l_id), 0)
            n_th = str(len(date_groups[ds])) if len(date_groups[ds]) >= 2 else ""
            fname_val = f"{ds}-{part_num}" if part_num > 0 else ds

            new_row = {
                "완료": "False",
                "제목": data.get('title', ''),
                "파일명": fname_val,
                "날짜": ds,
                "시작시간": start_t,
                "방송길이": "",
                "주제": "",
                "part": str(part_num) if part_num > 0 else "",
                "상세내용": "",
                "메모": "",
                "URL": "",
                "손님": "",
                "영도": "",
                "주식": "",
                "이미지파일명": "",
                "카테고리": data.get('category', ''),
                "고유 라이브 ID": l_id,
                "금일 방송 횟수": n_th,
                "타임라인CSV": "",
                "소스CSV": ""
            }
            df_new = pd.DataFrame([new_row])[CSV_HEADER]
            write_header = not os.path.exists(DB_FILE)
            df_new.to_csv(DB_FILE, mode='a', header=write_header, index=False, encoding='utf-8-sig')
            existing_ids.append(l_id)
        except Exception:
            continue

# 세션당 1회 실행 (삭제 직후 즉시 재등록 방지)
if 'auto_sync_checked' not in st.session_state:
    auto_sync_live_info()
    st.session_state.auto_sync_checked = True


# --- [JSON 데이터 관리 함수] ---
def load_details_db():
    if os.path.exists(DETAILS_FILE):
        try:
            with open(DETAILS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return {}
    return {}

def save_details_db(data):
    with open(DETAILS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_registry() -> dict:
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_registry(registry: dict) -> None:
    os.makedirs(TIMELINE_SAVE_DIR, exist_ok=True)
    with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def _registry_rename(old_csv_name: str, new_csv_name: str) -> None:
    """타임라인 CSV 리네임 시 레지스트리 current_name과 rename_log를 갱신한다."""
    registry = load_registry()
    for entry in registry.values():
        if entry.get("current_name") == old_csv_name:
            entry["rename_log"].append({
                "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "from": old_csv_name,
                "to": new_csv_name,
            })
            entry["current_name"] = new_csv_name
    save_registry(registry)


def _tl_basename(raw: str) -> str:
    """윈도우/맥 전체 경로 또는 파일명에서 파일명만 추출."""
    if not raw:
        return ""
    # 윈도우 경로 구분자가 있으면 뒤에서 분리
    raw = raw.replace("\\", "/")
    return os.path.basename(raw)


def _rename_timeline_file(src_name: str, canonical_name: str, entry, registry) -> bool:
    """타임라인 폴더 내 파일을 canonical_name으로 리네임. 성공 시 True."""
    import datetime as _dt
    if src_name == canonical_name:
        return True
    src_path = os.path.join(TIMELINE_SAVE_DIR, src_name)
    dst_path = os.path.join(TIMELINE_SAVE_DIR, canonical_name)
    if not os.path.exists(src_path):
        return False
    try:
        os.rename(src_path, dst_path)
        if entry is not None:
            entry.setdefault("rename_log", []).append({
                "from": src_name,
                "to": canonical_name,
                "at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })
            entry["current_name"] = canonical_name
            if registry is not None:
                save_registry(registry)
        return True
    except Exception as _e:
        st.warning(f"파일 리네임 실패 ({src_name} → {canonical_name}): {_e}")
        return False


def auto_match_stt(df_db: "pd.DataFrame") -> tuple:
    """broadcast_log 전체를 스캔해 {파일명}-타임라인.csv 규칙을 적용한다.
    - 레지스트리 항목: live_id 기반 매칭 + 리네임
    - 레지스트리 없는 기존 파일: 타임라인CSV 컬럼의 파일명으로 직접 리네임
    Returns (updated_df, matched_count)."""
    registry = load_registry()
    updated = 0

    # ── 1패스: 레지스트리 live_id 기반 ─────────────────────────────────────────
    for live_id, entry in registry.items():
        current_name = entry.get("current_name", "")
        if not current_name:
            continue
        mask = df_db["고유 라이브 ID"].astype(str).str.strip() == str(live_id).strip()
        if not mask.any():
            continue
        idx = df_db[mask].index[0]

        record_fname = str(df_db.at[idx, "파일명"]).strip() if "파일명" in df_db.columns else ""
        canonical_name = f"{record_fname}-타임라인.csv" if record_fname else current_name

        existing_col = _tl_basename(str(df_db.at[idx, "타임라인CSV"]).strip())
        if existing_col == canonical_name and os.path.exists(os.path.join(TIMELINE_SAVE_DIR, canonical_name)):
            continue  # 이미 완벽히 맞춰진 경우

        # canonical 파일이 이미 있으면 리네임 불필요, 없으면 current_name 또는 existing_col에서 이동
        if not os.path.exists(os.path.join(TIMELINE_SAVE_DIR, canonical_name)):
            for src_name in dict.fromkeys([current_name, existing_col]):
                if src_name and _rename_timeline_file(src_name, canonical_name, entry, registry):
                    break
        else:
            if entry.get("current_name") != canonical_name:
                entry["current_name"] = canonical_name
                save_registry(registry)

        df_db.at[idx, "타임라인CSV"] = canonical_name
        updated += 1

    # ── 2패스: 레지스트리 없는 기존 파일 (live_id 기록 없는 구버전 STT) ──────────
    for idx, row in df_db.iterrows():
        record_fname = str(row.get("파일명", "")).strip()
        if not record_fname:
            continue
        canonical_name = f"{record_fname}-타임라인.csv"

        existing_col_raw = str(row.get("타임라인CSV", "")).strip()
        existing_col = _tl_basename(existing_col_raw)
        if not existing_col:
            continue  # 타임라인CSV 자체가 없는 행은 건드리지 않음

        # 이미 정식 이름이면 컬럼값만 파일명으로 정규화
        if existing_col == canonical_name:
            if existing_col_raw != canonical_name:
                df_db.at[idx, "타임라인CSV"] = canonical_name
                updated += 1
            continue

        # 파일 리네임
        if not os.path.exists(os.path.join(TIMELINE_SAVE_DIR, canonical_name)):
            if _rename_timeline_file(existing_col, canonical_name, None, None):
                df_db.at[idx, "타임라인CSV"] = canonical_name
                updated += 1
        else:
            # canonical 이미 존재 → 컬럼만 갱신
            df_db.at[idx, "타임라인CSV"] = canonical_name
            updated += 1

    return df_db, updated


def _write_normalize_log(session_id: str, entries: list):
    """정규화 변경 내역을 JSONL 파일에 기록한다."""
    import json as _json
    with open(NORMALIZE_LOG, "a", encoding="utf-8") as f:
        for e in entries:
            e["session"] = session_id
            f.write(_json.dumps(e, ensure_ascii=False) + "\n")


def _safe_rename(src: str, dst: str) -> bool:
    """파일 리네임. 성공 시 True, src 없거나 dst 이미 있으면 False."""
    if not os.path.exists(src) or os.path.exists(dst):
        return False
    try:
        os.rename(src, dst)
        return True
    except Exception:
        return False


def normalize_filenames(df_db):
    """broadcast_log 전체를 스캔해 파일명 규칙과 맞지 않는 파일을 일괄 수정한다.
    대상: 타임라인CSV, 소스CSV, 이미지파일명(그림), 레바짤(clip_log).
    모든 변경은 _normalize_log.jsonl에 기록되어 되돌리기 가능.
    Returns (updated_df, fixed_count, details)."""
    import datetime as _dt
    import json as _json

    session_id = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_entries = []
    fixed = 0
    details = []

    def record(kind, directory, old_name, new_name, broadcast, file_renamed):
        log_entries.append({
            "at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "dir": directory,
            "old": old_name,
            "new": new_name,
            "broadcast": broadcast,
            "file_renamed": file_renamed,
        })

    # clip_log 로드 (짤 처리용)
    cl_df = None
    cl_dirty = False
    if os.path.exists(CLIP_DB_FILE):
        try:
            cl_df = pd.read_csv(CLIP_DB_FILE, dtype=str).fillna("")
        except Exception:
            cl_df = None

    for idx, row in df_db.iterrows():
        record_fname = str(row.get("파일명", "")).strip()
        if not record_fname:
            continue
        expected_prefix = record_fname.split("_")[0]  # 날짜[-부] 부분

        # ── 타임라인 CSV ──────────────────────────────────────────────────────
        tl_raw = str(row.get("타임라인CSV", "")).strip()
        tl_cur = _tl_basename(tl_raw)
        if tl_cur:
            tl_expected = f"{record_fname}-타임라인.csv"
            if tl_cur != tl_expected:
                renamed = _safe_rename(
                    os.path.join(TIMELINE_SAVE_DIR, tl_cur),
                    os.path.join(TIMELINE_SAVE_DIR, tl_expected),
                )
                if renamed:
                    _registry_rename(tl_cur, tl_expected)
                final = tl_expected if (renamed or os.path.exists(os.path.join(TIMELINE_SAVE_DIR, tl_expected))) else tl_cur
                record("타임라인", TIMELINE_SAVE_DIR, tl_cur, final, record_fname, renamed)
                details.append(f"타임라인: {tl_cur} → {final}" + ("" if renamed else " (파일 없음, 컬럼만)"))
                df_db.at[idx, "타임라인CSV"] = final
                fixed += 1
            elif tl_raw != tl_cur:
                df_db.at[idx, "타임라인CSV"] = tl_cur
                record("타임라인(경로정규화)", TIMELINE_SAVE_DIR, tl_raw, tl_cur, record_fname, False)
                details.append(f"타임라인 경로→이름: ...{tl_cur}")
                fixed += 1

        # ── 소스 CSV ─────────────────────────────────────────────────────────
        src_raw = str(row.get("소스CSV", "")).strip()
        src_cur = _tl_basename(src_raw)
        if src_cur:
            src_expected = f"{record_fname}-소스.csv"
            if src_cur != src_expected:
                renamed = _safe_rename(
                    os.path.join(SOURCE_SAVE_DIR, src_cur),
                    os.path.join(SOURCE_SAVE_DIR, src_expected),
                )
                final = src_expected if (renamed or os.path.exists(os.path.join(SOURCE_SAVE_DIR, src_expected))) else src_cur
                record("소스", SOURCE_SAVE_DIR, src_cur, final, record_fname, renamed)
                details.append(f"소스: {src_cur} → {final}" + ("" if renamed else " (파일 없음, 컬럼만)"))
                df_db.at[idx, "소스CSV"] = final
                fixed += 1
            elif src_raw != src_cur:
                df_db.at[idx, "소스CSV"] = src_cur
                record("소스(경로정규화)", SOURCE_SAVE_DIR, src_raw, src_cur, record_fname, False)
                details.append(f"소스 경로→이름: ...{src_cur}")
                fixed += 1

        # ── 그림 (이미지파일명) ───────────────────────────────────────────────
        img_raw = str(row.get("이미지파일명", "")).strip()
        if img_raw:
            imgs = [n.strip() for n in img_raw.split(",") if n.strip()]
            new_imgs = []
            for img in imgs:
                img_prefix = img.split("_")[0] if "_" in img else ""
                if img_prefix and img_prefix != expected_prefix:
                    new_img = expected_prefix + img[len(img_prefix):]
                    renamed = _safe_rename(
                        os.path.join(ABS_DIR, img),
                        os.path.join(ABS_DIR, new_img),
                    )
                    final_img = new_img if renamed else img
                    record("그림", ABS_DIR, img, final_img, record_fname, renamed)
                    details.append(f"그림: {img} → {final_img}" + ("" if renamed else " (파일 없음)"))
                    new_imgs.append(final_img)
                    if renamed:
                        fixed += 1
                else:
                    new_imgs.append(img)
            new_img_str = ", ".join(new_imgs)
            if new_img_str != img_raw:
                df_db.at[idx, "이미지파일명"] = new_img_str

        # ── 레바짤 (clip_log) ─────────────────────────────────────────────────
        if cl_df is not None:
            # 방송분류가 record_fname인 행 또는 날짜+prefix가 일치하는 행
            mask = cl_df["방송분류"] == record_fname
            for ci in cl_df[mask].index:
                clip_name = str(cl_df.at[ci, "파일명"])
                clip_prefix = clip_name.split("_")[0] if "_" in clip_name else clip_name.rsplit(".", 1)[0]
                if clip_prefix != expected_prefix:
                    new_clip = expected_prefix + clip_name[len(clip_prefix):]
                    renamed = _safe_rename(
                        os.path.join(CLIP_SAVE_DIR, clip_name),
                        os.path.join(CLIP_SAVE_DIR, new_clip),
                    )
                    final_clip = new_clip if renamed else clip_name
                    record("짤", CLIP_SAVE_DIR, clip_name, final_clip, record_fname, renamed)
                    details.append(f"짤: {clip_name} → {final_clip}" + ("" if renamed else " (파일 없음)"))
                    cl_df.at[ci, "파일명"] = final_clip
                    cl_dirty = True
                    if renamed:
                        fixed += 1

    # clip_log 저장
    if cl_dirty and cl_df is not None:
        cl_df.to_csv(CLIP_DB_FILE, index=False, encoding="utf-8-sig")

    # 로그 기록
    if log_entries:
        _write_normalize_log(session_id, log_entries)

    return df_db, fixed, details, session_id


def revert_normalize_session(session_id: str) -> tuple:
    """특정 세션의 정규화를 되돌린다. Returns (reverted_count, errors)."""
    import json as _json
    if not os.path.exists(NORMALIZE_LOG):
        return 0, ["로그 파일 없음"]

    entries = []
    with open(NORMALIZE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = _json.loads(line)
                if e.get("session") == session_id:
                    entries.append(e)
            except Exception:
                pass

    reverted = 0
    errors = []
    # 역순으로 처리
    for e in reversed(entries):
        if not e.get("file_renamed"):
            continue
        src = os.path.join(e["dir"], e["new"])
        dst = os.path.join(e["dir"], e["old"])
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.rename(src, dst)
                reverted += 1
            except Exception as ex:
                errors.append(f"{e['new']} → {e['old']}: {ex}")
        else:
            errors.append(f"되돌리기 불가: {e['new']} (없거나 {e['old']} 이미 존재)")
    return reverted, errors


# --- [CSS: 스타일 설정] ---
st.markdown("""
    <style>
    div.stButton > button[kind="primary"] {
        background-color: #00aaff !important;
        color: white !important;
        border: None !important;
    }
    div.stButton > button[kind="primary"]:hover {
        background-color: #0088cc !important;
    }
    /* 개별 삭제 버튼 스타일 */
    .del-btn-container {
        display: flex;
        align-items: center;
        justify-content: center;
    }
    [data-testid="stHorizontalBlock"] img {
        object-fit: cover;
        height: 200px;
        width: 100%;
        border-radius: 5px;
    }
    .gallery-header {
        margin-bottom: 25px;
    }
    [data-testid="stFileUploader"] {
        padding-top: 0px;
    }
    [data-testid="stFileUploaderDropzone"] {
        padding: 0px 10px;
        min-height: 70px;
    }
    [data-testid="stFileUploaderDropzoneInstructions"] {
        display: none;
    }
    [data-testid="stFileUploaderDropzone"]::before {
        content: "파일 드래그 또는 클릭";
        font-size: 0.8rem;
        color: #555;
    }
    .memo-modal {
        position: fixed;
        top: 20%;
        left: 50%;
        transform: translate(-50%, -20%);
        background-color: white;
        padding: 20px;
        border-radius: 10px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.3);
        z-index: 9999;
        width: 60%;
        max-height: 70%;
        overflow-y: auto;
        border: 1px solid #ddd;
    }
    </style>
    """, unsafe_allow_html=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
                conf = json.load(f)
                if conf and "categories" in conf:
                    return conf
        except:
            pass
    default_config = {
        "categories": ["그림 @소속,캐릭터,스킨", "원고 @작품명,회차,상태", "먹방 @메뉴,가게이름", "게임 @게임명,서버/종족", "술먹방 @주종,안주", "광고 @광고주,제품명", "레방", "야방", "노래방 @곡명", "VR", "합방 @멤버", "실수"],
        "drawing_types": ["신청/투표", "손풀이", "낙서", "커미션", "외주"]
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8-sig') as f:
        json.dump(default_config, f, ensure_ascii=False, indent=4)
    return default_config

def get_past_data(column_name):
    """DB와 JSON 상세 데이터에서 해당 필드(column_name)와 일치하는 과거 값만 추출"""
    all_vals = []
    
    # 1. DB 파일 기본 컬럼(손님 등) 체크
    if os.path.exists(DB_FILE):
        try:
            df = pd.read_csv(DB_FILE, dtype=str).fillna("")
            if column_name in df.columns:
                for val in df[column_name]:
                    all_vals.extend([v.strip() for v in str(val).split(",") if v.strip()])
        except: pass

    # 2. JSON 상세 데이터에서 해당 필드 이름과 일치하는 값만 추출
    details_db = load_details_db()
    config_data = load_config()
    
    for cat_opt in config_data["categories"]:
        if "@" in cat_opt:
            cat_main = cat_opt.split(" @")[0]
            fields = cat_opt.split("@")[1].split(",")
            if column_name in fields:
                f_idx = fields.index(column_name)
                for log_id in details_db:
                    cat_data_list = details_db[log_id].get(cat_main, [])
                    for entry in cat_data_list:
                        vals = entry.get("vals", [])
                        if len(vals) > f_idx and vals[f_idx].strip():
                            all_vals.append(vals[f_idx].strip())
                            
    return sorted(list(set(all_vals)))

def format_time_input(t_str):
    # [1. 시작시간 데이터 포맷 손실 문제 수정] HH:MM:SS 포맷을 그대로 유지하도록 변경
    # 콜론(:)을 제거하지 않도록 변경
    t_str = re.sub(r'[^0-9:]', '', t_str) 
    
    # 4자리 숫자(HHMM)일 경우에만 콜론 추가, 이미 콜론이 있거나 다른 형태면 그대로 반환
    if len(re.sub(r'[^0-9]', '', t_str)) == 4 and ':' not in t_str:
        return f"{t_str[:2]}:{t_str[2:]}"
    elif len(re.sub(r'[^0-9]', '', t_str)) == 6 and ':' not in t_str:
        return f"{t_str[:2]}:{t_str[2:4]}:{t_str[4:]}"
    return t_str

def reset_form():
    st.session_state.form_data = {
        "date": datetime.now(),
        "part": 0,
        "start_t": "",
        "dur": "",
        "title": "",
        "url": "",
        "timeline_csv": "",
        "source_csv": "",
        "topics": [],
        "details": "",
        "memo": "",
        "guest": [],
        "v_tube": False,
        "stock": False,
        "is_done": False,
        "parsed_details": {},
        "edit_mode": False,
        "edit_index": None
    }
    if 'clip_items' not in st.session_state:
        st.session_state.clip_items = []
    st.session_state.drawing_count = 1
    config_data = load_config()
    for cat in config_data["categories"]:
        cat_name = cat.split(" @")[0]
        st.session_state[f"c_{cat_name}"] = 1
    # 짤 목록 항상 초기화
    st.session_state.clip_items = []
    st.session_state.clip_uploader_key = st.session_state.get('clip_uploader_key', 0) + 1

def fetch_vod_api(size=50):
    all_videos = []
    page = 0  # 1페이지(0)부터 시작해서 무한으로 넘깁니다
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Referer': 'https://chzzk.naver.com/'
    }
    
    # 💡 치지직 서버가 "더 이상 VOD 없어!" 할 때까지 무한 반복
    while True:
        # url에 페이지(page) 파라미터를 추가해서 과거로 거슬러 올라갑니다
        url = f"https://api.chzzk.naver.com/service/v1/channels/{CHANNEL_ID}/videos?sortType=LATEST&page={page}&size={size}"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json().get('content', {}).get('data', [])
                
                # 가져온 데이터가 텅 비었다면? (마지막 페이지 도달) -> 탈출!
                if not data:
                    break
                    
                all_videos.extend(data) # 가져온 50개를 전체 바구니에 담기
                page += 1 # 다음 페이지로 넘어가기!
                
                # 🛡️ 방어막 회피: 치지직 서버가 공격으로 오해하지 않도록 0.5초 대기
                time.sleep(0.5) 
            else:
                break # 정상 응답이 아니면 즉시 중단
        except:
            break # 에러(인터넷 끊김 등) 발생 시 중단
            
    return all_videos

_CHOSUNG  = ['ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ','ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ']
_JUNGSUNG = ['ㅏ','ㅐ','ㅑ','ㅒ','ㅓ','ㅔ','ㅕ','ㅖ','ㅗ','ㅘ','ㅙ','ㅚ','ㅛ','ㅜ','ㅝ','ㅞ','ㅟ','ㅠ','ㅡ','ㅢ','ㅣ']
_JONGSUNG = ['','ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ','ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ']
_CHOSUNG_SET = set(_CHOSUNG)

def _decompose(text):
    """한글을 자모 단위로 분리 (로켓 → ㄹㅗㅋㅔㅅ)"""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code <= 11171:
            out.append(_CHOSUNG[code // 588])
            out.append(_JUNGSUNG[(code % 588) // 28])
            jong = _JONGSUNG[code % 28]
            if jong:
                out.append(jong)
        else:
            out.append(ch)
    return ''.join(out)

def _get_chosung(text):
    """텍스트의 초성만 추출 (로켓랩 → ㄹㅋㄹ)"""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code <= 11171:
            out.append(_CHOSUNG[code // 588])
        elif ch in _CHOSUNG_SET:
            out.append(ch)
    return ''.join(out)

def _is_chosung_only(text):
    stripped = text.replace(' ', '')
    return bool(stripped) and all(c in _CHOSUNG_SET for c in stripped)

def search_csv_files(folder_dir, keywords, fuzzy=False, threshold=75):
    results = []
    if not os.path.exists(folder_dir):
        return results
    csv_files = sorted([f for f in os.listdir(folder_dir) if f.endswith('.csv')], reverse=True)
    for csv_file in csv_files:
        file_path = os.path.join(folder_dir, csv_file)
        try:
            df = pd.read_csv(file_path, dtype=str, comment='#').fillna("")
            # 헤더 없는 CSV 감지: 열 이름이 시간(HH:MM:SS) 패턴이면 첫 행이 데이터임
            if any(re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', str(c)) for c in df.columns):
                df = pd.read_csv(file_path, header=None, dtype=str, comment='#').fillna("")
                _n = len(df.columns)
                _fallback = ['시간', '내용'] + [f'열{i+3}' for i in range(max(0, _n - 2))]
                df.columns = _fallback[:_n]
            matched_rows = []
            for _, row in df.iterrows():
                row_str = " ".join(str(v) for v in row.values if str(v).strip())
                hit_kws = []
                for kw in keywords:
                    if not kw:
                        continue
                    if _is_chosung_only(kw):
                        # 초성 검색: 공백 제거 후 초성열에서 패턴 탐색
                        if kw.replace(' ', '') in _get_chosung(row_str):
                            hit_kws.append(f"{kw}(초성)")
                    elif fuzzy and rfuzz:
                        # 자모 분리 후 유사도 비교 (STT 오타에 강함)
                        score = rfuzz.partial_ratio(
                            _decompose(kw.lower()),
                            _decompose(row_str.lower())
                        )
                        if score >= threshold:
                            hit_kws.append(f"{kw}({score}%)")
                    else:
                        if kw.lower() in row_str.lower():
                            hit_kws.append(kw)
                if hit_kws:
                    matched_rows.append({"매칭키워드": ", ".join(hit_kws), **row.to_dict()})
            if matched_rows:
                results.append({"file": csv_file, "rows": matched_rows})
        except Exception:
            continue
    return results

config = load_config()
st.set_page_config(page_title="REVA 기록기", layout="wide")

if 'temp_buffer' not in st.session_state:
    st.session_state.temp_buffer = []
if 'form_data' not in st.session_state:
    reset_form()
if 'drawing_count' not in st.session_state:
    st.session_state.drawing_count = 1
if 'menu_idx' not in st.session_state:
    st.session_state.menu_idx = 0
if 'merged_temp_df' not in st.session_state:
    st.session_state.merged_temp_df = None
if 'show_memo_info' not in st.session_state:
    st.session_state.show_memo_info = None

for opt in config["categories"]:
    c_name = opt.split(" @")[0]
    if f"c_{c_name}" not in st.session_state:
        st.session_state[f"c_{c_name}"] = 1

def on_menu_change():
    st.session_state.menu_idx = menu_options.index(st.session_state.menu_selection)

if 'clip_items' not in st.session_state:
    st.session_state.clip_items = []

if st.session_state.form_data.get("edit_mode", False):
    with st.sidebar:
        st.markdown("**📝 수정 모드**")
        sidebar_save = st.button("✅ 수정 완료", key="sidebar_save_btn", type="primary", use_container_width=True)
        sidebar_cancel = st.button("❌ 수정 취소", key="sidebar_cancel_btn", use_container_width=True)
        st.markdown("---")
else:
    sidebar_save = False
    sidebar_cancel = False

st.sidebar.markdown("###")
menu_options = (
    ["레바실록", "레바 그림 갤러리", "레바 짤 갤러리", "타임라인 검색"]
    if PUBLIC_MODE
    else ["입력", "레바실록", "레바 그림 갤러리", "레바 짤 갤러리", "소스 검색", "타임라인 검색", "카테고리"]
)
menu = st.sidebar.radio("📌 메뉴 선택", menu_options, index=st.session_state.menu_idx, key="menu_selection", on_change=on_menu_change)

RENAME_MAP = {
    "완료": "완료",
    "제목": "제목",
    "파일명": "분류",
    "날짜": "날짜",
    "시작시간": "시간",
    "방송길이": "길이",
    "주제": "주제",
    "part": "N부",
    "상세내용": "상세",
    "메모": "메모",
    "URL": "다시보기",
    "타임라인CSV": "타임라인CSV",
    "소스CSV": "소스CSV",
    "손님": "손님",
    "영도": "영도",
    "주식": "주식",
    "이미지파일명": "그림"
}

if menu == "입력":
    st.title("💾 방송 데이터 입력" if not st.session_state.form_data["edit_mode"] else "📝 데이터 수정 중")

    if st.session_state.form_data["edit_mode"]:
        top_save_container = st.container()

    with st.expander("🤖 치지직 VOD 데이터 가져오기", expanded=False):
        cv1, cv2, cv3 = st.columns([1, 1, 1])
        sd_api = cv1.date_input("조회 시작일", value=datetime.now() - timedelta(days=7))
        ed_api = cv2.date_input("조회 종료일", value=datetime.now())
        
        if cv3.button("📡 VOD 목록 불러오기", use_container_width=True):
            st.session_state.vod_list = fetch_vod_api(50)
            
        if 'vod_list' in st.session_state:
            for v in st.session_state.vod_list:
                try:
                    clean_date = v['publishDate'].replace('T', ' ').split('.')[0]
                    v_dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S")
                    
                    if sd_api <= v_dt.date() <= ed_api:
                        col_i, col_b = st.columns([4, 1.2])
                        col_i.write(f"📅 {v_dt.strftime('%m-%d %H:%M')} | {v['videoTitle']}")
                        
                        # [VOD 데이터 선택적 적용 로직]
                        if col_b.button("🔗 VOD 연결", key=f"v_inp_{v['videoNo']}"):
                            # VOD의 고유 식별자 추출 (너구리의 l_id 로직 적용)
                            # VOD API에서는 'videoNo'가 핵심이지만, 내부 매칭용 ID가 따로 있다면 이를 활용합니다.
                            vod_id = str(v.get('videoNo', ''))
                            vod_duration = str(timedelta(seconds=v.get('duration', 0))).zfill(8)
                            vod_url = f"https://chzzk.naver.com/video/{v['videoNo']}"
    
                            # [핵심] 기존 form_data 중 dur와 url만 업데이트
                            st.session_state.form_data.update({
                                "dur": vod_duration,
                                "url": vod_url
                                # "title", "date", "start_t" 등은 건드리지 않음으로써 실시간 데이터 보호
                            })
                            
                            st.success(f"✅ VOD 정보(길이/URL)만 연동되었습니다. (ID: {vod_id})")
                            st.rerun()
                except:
                    continue


    with st.expander("➕ 방송 상세 내용 작성", expanded=True):
        f1, f2, f3 = st.columns([1, 1, 2])
        date_in = f1.date_input("방송 날짜", value=st.session_state.form_data["date"])
        part_in = f1.number_input("n부", min_value=0, value=int(st.session_state.form_data["part"]))
        raw_time = f2.text_input("시작 시간", value=st.session_state.form_data["start_t"], autocomplete="off")
        time_in = format_time_input(raw_time)
        dur_in = f2.text_input("방송 길이", value=st.session_state.form_data["dur"], autocomplete="off")
        title_in = f3.text_input("방송 제목", value=st.session_state.form_data["title"], autocomplete="off")
        url_in = f3.text_input("VOD URL", value=st.session_state.form_data["url"], autocomplete="off")

        st.divider()
        # ── 타임라인 CSV ──────────────────────────────────────────
        st.markdown("**📊 타임라인 CSV**")
        tl_existing = st.session_state.form_data.get("timeline_csv", "")
        tl_c1, tl_c2 = st.columns([4, 1])
        uploaded_tl = tl_c1.file_uploader("타임라인 CSV/xlsx 선택", type=["csv", "xlsx", "xls"], key="timeline_csv_uploader", label_visibility="collapsed")
        if uploaded_tl is not None:
            ds_tmp = date_in.strftime('%Y%m%d')
            fname_tmp = ds_tmp
            if part_in > 0: fname_tmp += f"-{part_in}"
            tl_save_name = f"{fname_tmp}-타임라인.csv"
            tl_save_path = os.path.join(TIMELINE_SAVE_DIR, tl_save_name)
            os.makedirs(TIMELINE_SAVE_DIR, exist_ok=True)
            try:
                already_in_dir = os.path.join(TIMELINE_SAVE_DIR, uploaded_tl.name)
                if os.path.exists(already_in_dir) and already_in_dir != tl_save_path:
                    # 이미 타임라인 폴더 안에 있는 파일 → 복사 없이 리네임만
                    os.rename(already_in_dir, tl_save_path)
                    _registry_rename(uploaded_tl.name, tl_save_name)
                elif not os.path.exists(tl_save_path):
                    if uploaded_tl.name.lower().endswith(('.xlsx', '.xls')):
                        import io
                        _df_tl = pd.read_excel(io.BytesIO(uploaded_tl.getvalue()), header=None)
                        for _col in _df_tl.columns:
                            _df_tl[_col] = _df_tl[_col].apply(
                                lambda v: v.strftime('%H:%M:%S') if hasattr(v, 'strftime') else ('' if pd.isna(v) else str(v))
                            )
                        _df_tl = _df_tl[[c for c in _df_tl.columns if _df_tl[c].str.strip().any()]]
                        _df_tl.to_csv(tl_save_path, index=False, header=False, encoding='utf-8-sig')
                    else:
                        with open(tl_save_path, "wb") as _f:
                            _f.write(uploaded_tl.getvalue())
            except Exception as e:
                st.error(f"파일 저장 오류: {e}")
                tl_save_path = tl_existing
            timeline_csv_in = tl_save_path
            st.caption(f"✅ 저장: {tl_save_name}")
        else:
            timeline_csv_in = tl_existing
            if tl_existing:
                st.caption(f"📎 기존: {os.path.basename(tl_existing)}")
                if tl_c2.button("🗑️ 삭제", key="del_tl"):
                    if os.path.exists(tl_existing):
                        os.remove(tl_existing)
                    st.session_state.form_data["timeline_csv"] = ""
                    timeline_csv_in = ""
                    st.rerun()

        # ── 소스 CSV ─────────────────────────────────────────────
        st.markdown("**🎬 소스 CSV**")
        src_existing = st.session_state.form_data.get("source_csv", "")
        src_c1, src_c2 = st.columns([4, 1])
        uploaded_src = src_c1.file_uploader("소스 CSV/xlsx 선택", type=["csv", "xlsx", "xls"], key="source_csv_uploader", label_visibility="collapsed")
        if uploaded_src is not None:
            ds_tmp = date_in.strftime('%Y%m%d')
            fname_tmp = ds_tmp
            if part_in > 0: fname_tmp += f"-{part_in}"
            src_save_name = f"{fname_tmp}-소스.csv"
            src_save_path = os.path.join(SOURCE_SAVE_DIR, src_save_name)
            os.makedirs(SOURCE_SAVE_DIR, exist_ok=True)
            try:
                if uploaded_src.name.lower().endswith(('.xlsx', '.xls')):
                    import io
                    _df_src = pd.read_excel(io.BytesIO(uploaded_src.getvalue()), header=None)
                    for _col in _df_src.columns:
                        _df_src[_col] = _df_src[_col].apply(
                            lambda v: v.strftime('%H:%M:%S') if hasattr(v, 'strftime') else ('' if pd.isna(v) else str(v))
                        )
                    _df_src = _df_src[[c for c in _df_src.columns if _df_src[c].str.strip().any()]]
                    # 소스 CSV 헤더: A=시간, B=내용, 그 이후=열3,열4,...
                    _n = len(_df_src.columns)
                    _src_hdr = ['시간', '내용'] + [f'열{i+3}' for i in range(max(0, _n - 2))]
                    _df_src.columns = _src_hdr[:_n]
                    _df_src.to_csv(src_save_path, index=False, header=True, encoding='utf-8-sig')
                else:
                    with open(src_save_path, "wb") as _f:
                        _f.write(uploaded_src.getvalue())
            except Exception as e:
                st.error(f"파일 저장 오류: {e}")
                src_save_path = src_existing
            source_csv_in = src_save_path
            st.caption(f"✅ 저장: {src_save_name}")
        else:
            source_csv_in = src_existing
            if src_existing:
                st.caption(f"📎 기존: {os.path.basename(src_existing)}")
                if src_c2.button("🗑️ 삭제", key="del_src"):
                    if os.path.exists(src_existing):
                        os.remove(src_existing)
                    st.session_state.form_data["source_csv"] = ""
                    source_csv_in = ""
                    st.rerun()

        st.divider()
        # ── 레바 짤 ──────────────────────────────────────────────
        st.markdown("**🖼️ 레바 짤**")

        if 'clip_uploader_key' not in st.session_state:
            st.session_state.clip_uploader_key = 0

        uploaded_clips = st.file_uploader(
            "짤 파일 선택 (복수 선택 가능)",
            type=["png","jpg","jpeg","gif","webp"],
            accept_multiple_files=True,
            key=f"clip_uploader_{st.session_state.clip_uploader_key}",
            label_visibility="collapsed"
        )

        # 새 파일이 선택되면 즉시 bytes로 clip_items에 추가하고 uploader 초기화
        if uploaded_clips:
            added = False
            for uc in uploaded_clips:
                already = any(c["orig_name"] == uc.name for c in st.session_state.clip_items)
                if not already:
                    st.session_state.clip_items.append({
                        "orig_name": uc.name,
                        "data": uc.getvalue(),   # bytes 즉시 저장
                        "ext": os.path.splitext(uc.name)[1].lower(),
                        "memo": ""
                    })
                    added = True
            if added:
                # uploader key 변경으로 초기화 → bytes는 clip_items에 안전하게 보존됨
                st.session_state.clip_uploader_key += 1
                st.rerun()

        if st.session_state.clip_items:
            st.caption(f"추가된 짤: {len(st.session_state.clip_items)}개")
            for ci_idx in range(len(st.session_state.clip_items)):
                ci = st.session_state.clip_items[ci_idx]
                ci_col1, ci_col2, ci_col3 = st.columns([3, 4, 1])
                ci_col1.caption(ci["orig_name"])
                new_memo = ci_col2.text_input(
                    f"메모_{ci_idx}",
                    value=ci.get("memo", ""),
                    placeholder="간단한 메모 (파일명에 포함)",
                    label_visibility="collapsed",
                    key=f"clip_memo_{ci_idx}",
                    autocomplete="off"
                )
                # 메모 변경을 clip_items에 즉시 반영
                st.session_state.clip_items[ci_idx]["memo"] = new_memo
                if ci_col3.button("❌", key=f"del_clip_{ci_idx}"):
                    st.session_state.clip_items.pop(ci_idx)
                    st.rerun()
        st.divider()
        
        memo_in = st.text_area("📝 방송 메모 (입력)", value=str(st.session_state.form_data.get("memo", "")), placeholder="타임라인이나 대화 스크립트를 기록하세요.")

        st.divider()
        sub1, sub2, sub3, sub4 = st.columns([1, 1, 1, 1])
        guest_list = st.session_state.form_data.get("guest", [])
        has_g = sub1.checkbox("👥 손님 포함", value=len(guest_list) > 0)
        has_v = sub2.checkbox("📺 영도", value=st.session_state.form_data.get("v_tube", False))
        has_s = sub3.checkbox("📈 주식", value=st.session_state.form_data.get("stock", False))
        is_done_in = sub4.checkbox("✅ 방송 기록 완료", value=st.session_state.form_data.get("is_done", False))
        
        past_guests = get_past_data("손님")
        g_final = sub1.multiselect("기존 손님", past_guests, default=[g for g in guest_list if g in past_guests]) if has_g else []
        if has_g:
            new_g = sub1.text_input("새 손님 (콤마 구분)", autocomplete="off")
            if new_g:
                g_final += [x.strip() for x in new_g.split(",") if x.strip()]
            
        st.divider()
        main_opts = [o.split(" @")[0] for o in config["categories"]]
        saved_topics = st.session_state.form_data.get("topics", [])
        final_topics = st.multiselect("주제 선택", main_opts, default=[t for t in saved_topics if t in main_opts])
        
        details_list = []
        images_buffer = []
        parsed_store = st.session_state.form_data.get("parsed_details", {})
        
        current_parsed_details = {}

        for opt in config["categories"]:
            cat_name = opt.split(" @")[0]
            if cat_name in final_topics:
                st.markdown(f"#### 📍 {cat_name}")
                fields = opt.split("@")[1].split(",") if "@" in opt else []
                hist_data = parsed_store.get(cat_name, [])
                
                if cat_name == "그림":
                    repeat_count = st.session_state.drawing_count
                else:
                    if f"c_{cat_name}" not in st.session_state:
                        st.session_state[f"c_{cat_name}"] = 1
                    repeat_count = st.session_state[f"c_{cat_name}"]
                
                current_parsed_details[cat_name] = []
                
                # 삭제 대상을 추적하기 위한 리스트 (세션 상태 활용)
                if f"del_{cat_name}" not in st.session_state:
                    st.session_state[f"del_{cat_name}"] = []

                for i in range(repeat_count):
                    # 삭제된 인덱스인 경우 건너뛰기
                    if i in st.session_state[f"del_{cat_name}"]:
                        continue

                    # 그리드 구성 (삭제 버튼용 컬럼 추가)
                    base_cols = len(fields) + 4 if cat_name == "그림" else len(fields) + 1
                    cols = st.columns(base_cols)
                    
                    row_d = []
                    d_meta = {}
                    ci = 0
                    existing = hist_data[i] if i < len(hist_data) else {}
                    vals = existing.get('vals', [])
                    
                    if cat_name == "그림":
                        dt_v = existing.get('type', config["drawing_types"][0])
                        d_meta["type"] = cols[ci].selectbox(f"분류_{i}", config["drawing_types"], index=config["drawing_types"].index(dt_v) if dt_v in config["drawing_types"] else 0, key=f"dt_{i}", label_visibility="collapsed")
                        ci += 1
                        for f_idx, f in enumerate(fields):
                            v = vals[f_idx] if f_idx < len(vals) else ""
                            p_o = get_past_data(f)
                            sv = cols[ci].selectbox(f"{f}_{i}", ["직접 입력"] + p_o, index=p_o.index(v)+1 if v in p_o else 0, key=f"s_{i}_{f}", label_visibility="collapsed")
                            iv = cols[ci].text_input(f"입력 {f}_{i}", value=v if sv == "직접 입력" else "", key=f"i_{i}_{f}", label_visibility="collapsed", autocomplete="off").strip()
                            row_d.append(iv if sv == "직접 입력" else sv)
                            ci += 1
                        d_meta["vt"] = cols[ci].checkbox(f"🎬 버튜버_{i}", value=existing.get('vt', False), key=f"vt_{i}")
                        img_f = cols[ci+1].file_uploader(f"그림_{i}", type=['png','jpg','jpeg'], key=f"im_{i}", label_visibility="collapsed")
                        
                        body_list = []
                        if d_meta["vt"]:
                            body_list.append("버튜버")
                        body_list.extend([x.strip() for x in row_d if x.strip()])
                        d_meta["body_parts"] = body_list
                        
                        existing_img = existing.get('img_name', "")
                        if existing_img:
                            cols[ci+1].caption(f"기존: {existing_img}")
                        
                        if img_f:
                            fb = img_f.read()
                            d_meta["file_data"] = fb
                            d_meta["ext"] = os.path.splitext(img_f.name)[1]
                            images_buffer.append(d_meta)
                        elif existing_img:
                            d_meta["existing_file"] = existing_img
                            images_buffer.append(d_meta)
                        else:
                            images_buffer.append({"empty": True})
                        
                        # 개별 삭제 버튼 (맨 끝 컬럼)
                        if cols[ci+2].button("❌", key=f"del_item_{cat_name}_{i}"):
                            # 실제 삭제 로직: parsed_store에서 해당 인덱스 제거 및 세션 개수 조정
                            if cat_name in parsed_store and len(parsed_store[cat_name]) > i:
                                parsed_store[cat_name].pop(i)
                            if cat_name == "그림": st.session_state.drawing_count = max(1, st.session_state.drawing_count - 1)
                            else: st.session_state[f"c_{cat_name}"] = max(1, repeat_count - 1)
                            st.rerun()

                        rs = "-".join(filter(None, row_d))
                        if rs:
                            details_list.append(f"{'버튜버 ' if d_meta.get('vt') else ''}{d_meta['type']} {rs}")
                            current_parsed_details[cat_name].append({"vals": row_d, "vt": d_meta["vt"], "type": d_meta["type"], "img_name": existing_img})
                    
                    else:
                        for f_idx, f in enumerate(fields):
                            v = vals[f_idx] if f_idx < len(vals) else ""
                            p_o = get_past_data(f)
                            sv = cols[ci].selectbox(f"{f}_{cat_name}_{i}", ["직접 입력"] + p_o, index=p_o.index(v)+1 if v in p_o else 0, key=f"s_{cat_name}_{i}_{f}", label_visibility="collapsed")
                            iv = cols[ci].text_input(f"입력 {f}_{cat_name}_{i}", value=v if sv == "직접 입력" else "", key=f"i_{cat_name}_{i}_{f}", label_visibility="collapsed", autocomplete="off").strip()
                            row_d.append(iv if sv == "직접 입력" else sv)
                            ci += 1
                        
                        # 개별 삭제 버튼
                        if cols[ci].button("❌", key=f"del_item_{cat_name}_{i}"):
                            if cat_name in parsed_store and len(parsed_store[cat_name]) > i:
                                parsed_store[cat_name].pop(i)
                            st.session_state[f"c_{cat_name}"] = max(1, repeat_count - 1)
                            st.rerun()

                        rs = "-".join(filter(None, row_d))
                        if rs:
                            details_list.append(f"{cat_name}: {rs}")
                            current_parsed_details[cat_name].append({"vals": row_d})

                b1, _ = st.columns([1, 9])
                if b1.button(f"➕ {cat_name} 추가", key=f"add_btn_{cat_name}"):
                    if cat_name == "그림": st.session_state.drawing_count += 1
                    else: st.session_state[f"c_{cat_name}"] = repeat_count + 1
                    st.rerun()

        if not st.session_state.form_data["edit_mode"]:
            if st.button("➕ 임시 테이블에 추가", use_container_width=True, type="primary"):
                ds = date_in.strftime('%Y%m%d')
                final_fname = ds
                if part_in > 0:
                    final_fname += f"-{part_in}"
                if final_topics:
                    final_fname += f"_{'_'.join(final_topics)}"
                if g_final:
                    final_fname += f"_{'_'.join(g_final)}"
                
                new_entry = {
                    "완료": is_done_in,
                    "제목": title_in,
                    "분류": final_fname,
                    "날짜": ds,
                    "시간": time_in,
                    "길이": dur_in,
                    "주제": ", ".join(final_topics),
                    "N부": str(part_in) if part_in > 0 else "",
                    "상세": " | ".join(details_list),
                    "메모": str(memo_in),
                    "다시보기": url_in,
                    "타임라인CSV": timeline_csv_in,
                    "소스CSV": source_csv_in,
                    "손님": ", ".join(g_final),
                    "영도": "O" if has_v else "",
                    "주식": "O" if has_s else "",
                    "그림": "",
                    "_img_data": list(images_buffer),
                    "_part_val": part_in,
                    "_json_meta": current_parsed_details
                }
                # 레바 짤 저장 — 메모는 위젯 세션 상태에서 직접 읽기
                clips_to_save = [c for c in st.session_state.clip_items if c.get("data")]
                for ci_idx, ci in enumerate(clips_to_save):
                    # text_input key로 저장된 최신 메모 값 읽기
                    clip_memo = str(st.session_state.get(f"clip_memo_{ci_idx}", ci.get("memo", ""))).strip()
                    clip_fname = ds
                    if part_in > 0: clip_fname += f"-{part_in}"
                    if clip_memo: clip_fname += f"_{clip_memo}"
                    clip_fname += ci["ext"]
                    clip_path = os.path.join(CLIP_SAVE_DIR, clip_fname)
                    os.makedirs(CLIP_SAVE_DIR, exist_ok=True)
                    with open(clip_path, "wb") as _f:
                        _f.write(ci["data"])
                    clip_row = pd.DataFrame([{
                        "날짜": ds,
                        "N부": str(part_in) if part_in > 0 else "",
                        "파일명": clip_fname,
                        "메모": clip_memo,
                        "방송분류": final_fname
                    }])
                    clip_row.to_csv(CLIP_DB_FILE, mode='a', header=not os.path.exists(CLIP_DB_FILE), index=False, encoding='utf-8-sig')
                st.session_state.clip_items = []
                st.session_state.clip_uploader_key = st.session_state.get('clip_uploader_key', 0) + 1
                st.session_state.temp_buffer.append(new_entry)
                reset_form()
                st.rerun()
        else:
            with top_save_container:
                col_save1, col_save2 = st.columns(2)
                save_clicked = col_save1.button("✅ 수정 완료 (DB 즉시 반영)", use_container_width=True, type="primary")
                cancel_clicked = col_save2.button("❌ 수정 취소", use_container_width=True)
            if save_clicked or sidebar_save:
                ds = date_in.strftime('%Y%m%d')
                final_fname = ds
                if part_in > 0:
                    final_fname += f"-{part_in}"
                if final_topics:
                    final_fname += f"_{'_'.join(final_topics)}"
                if g_final:
                    final_fname += f"_{'_'.join(g_final)}"

                img_names = []
                for info in images_buffer:
                    if info.get("empty"):
                        img_names.append("")
                        continue
                        
                    if "file_data" in info:
                        fn_base = ds
                        if part_in > 0:
                            fn_base += f"-{part_in}"
                        
                        body_parts = info.get("body_parts", [])
                        if body_parts:
                            nm = f"{fn_base}_{'_'.join(body_parts)}{info['ext']}"
                        else:
                            nm = f"{fn_base}{info['ext']}"
                            
                        with open(os.path.join(ABS_DIR, nm), "wb") as f:
                            f.write(info["file_data"])
                        img_names.append(nm)
                    elif "existing_file" in info:
                        img_names.append(info["existing_file"])
                    else:
                        img_names.append("")
                
                final_img_str = ", ".join([n for n in img_names if n])
                if "그림" in current_parsed_details:
                    for idx, entry in enumerate(current_parsed_details["그림"]):
                        if idx < len(img_names):
                            entry["img_name"] = img_names[idx]

                updated_row_dict = {
                    "완료": str(is_done_in),
                    "제목": title_in,
                    "파일명": final_fname,
                    "날짜": ds,
                    "시작시간": time_in,
                    "방송길이": dur_in,
                    "주제": ", ".join(final_topics),
                    "part": str(part_in) if part_in > 0 else "",
                    "상세내용": " | ".join(details_list),
                    "메모": str(memo_in),
                    "URL": url_in,
                    "타임라인CSV": timeline_csv_in,
                    "소스CSV": source_csv_in,
                    "손님": ", ".join(g_final),
                    "영도": "O" if has_v else "",
                    "주식": "O" if has_s else "",
                    "이미지파일명": final_img_str
                }
                
                df_db = pd.read_csv(DB_FILE, dtype=str).fillna("")

                # ── 파일명 변경 시 관련 파일 일괄 리네임 ──────────────────────
                old_fname = str(df_db.at[st.session_state.form_data["edit_index"], "파일명"]) if "파일명" in df_db.columns else ""
                old_prefix = old_fname.split("_")[0] if old_fname else ""
                new_prefix = final_fname.split("_")[0]

                if old_fname and old_fname != final_fname:
                    # 타임라인 CSV 리네임
                    tl_basename = _tl_basename(timeline_csv_in)
                    if tl_basename:
                        old_tl_path = os.path.join(TIMELINE_SAVE_DIR, tl_basename)
                        new_tl_name = final_fname + "-타임라인.csv"
                        new_tl_path = os.path.join(TIMELINE_SAVE_DIR, new_tl_name)
                        if os.path.exists(old_tl_path) and old_tl_path != new_tl_path:
                            os.rename(old_tl_path, new_tl_path)
                            _registry_rename(tl_basename, new_tl_name)
                        timeline_csv_in = new_tl_name
                        updated_row_dict["타임라인CSV"] = timeline_csv_in

                    # 소스 CSV 리네임
                    src_basename = _tl_basename(source_csv_in)
                    if src_basename:
                        old_src_path = os.path.join(SOURCE_SAVE_DIR, src_basename)
                        new_src_name = final_fname + "-소스.csv"
                        new_src_path = os.path.join(SOURCE_SAVE_DIR, new_src_name)
                        if os.path.exists(old_src_path) and old_src_path != new_src_path:
                            os.rename(old_src_path, new_src_path)
                        source_csv_in = new_src_name
                        updated_row_dict["소스CSV"] = source_csv_in

                    # 레바 그림 리네임 (날짜-부수 접두사만 교체)
                    renamed_imgs = []
                    for img_nm in [n.strip() for n in final_img_str.split(",") if n.strip()]:
                        if img_nm.startswith(old_prefix):
                            new_img_nm = new_prefix + img_nm[len(old_prefix):]
                            old_img_path = os.path.join(ABS_DIR, img_nm)
                            new_img_path = os.path.join(ABS_DIR, new_img_nm)
                            if os.path.exists(old_img_path) and old_img_path != new_img_path:
                                os.rename(old_img_path, new_img_path)
                            renamed_imgs.append(new_img_nm)
                        else:
                            renamed_imgs.append(img_nm)
                    final_img_str = ", ".join(renamed_imgs)
                    updated_row_dict["이미지파일명"] = final_img_str

                    # 레바짤 리네임 (날짜-부수 접두사만 교체) + clip_log 업데이트
                    if os.path.exists(CLIP_DB_FILE):
                        try:
                            cl_df = pd.read_csv(CLIP_DB_FILE, dtype=str).fillna("")
                            mask = cl_df["방송분류"] == old_fname
                            for ci in cl_df[mask].index:
                                old_clip = str(cl_df.at[ci, "파일명"])
                                if old_clip.startswith(old_prefix):
                                    new_clip = new_prefix + old_clip[len(old_prefix):]
                                    old_clip_path = os.path.join(CLIP_SAVE_DIR, old_clip)
                                    new_clip_path = os.path.join(CLIP_SAVE_DIR, new_clip)
                                    if os.path.exists(old_clip_path) and old_clip_path != new_clip_path:
                                        os.rename(old_clip_path, new_clip_path)
                                    cl_df.at[ci, "파일명"] = new_clip
                            cl_df.loc[mask, "방송분류"] = final_fname
                            cl_df.to_csv(CLIP_DB_FILE, index=False, encoding='utf-8-sig')
                        except Exception:
                            pass

                    # details_db 키 이전
                    all_meta = load_details_db()
                    if old_fname in all_meta:
                        all_meta[final_fname] = all_meta.pop(old_fname)
                        save_details_db(all_meta)
                # ─────────────────────────────────────────────────────────────

                for k, v in updated_row_dict.items():
                    df_db.at[st.session_state.form_data["edit_index"], k] = str(v)

                df_db[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                
                all_details = load_details_db()
                all_details[final_fname] = current_parsed_details
                save_details_db(all_details)

                # 수정 모드에서도 레바 짤 저장
                # 1) clip_log에서 이 방송분류의 기존 항목 제거 (중복 방지)
                if os.path.exists(CLIP_DB_FILE):
                    try:
                        cl_df = pd.read_csv(CLIP_DB_FILE, dtype=str).fillna("")
                        cl_df = cl_df[cl_df["방송분류"] != final_fname]
                        cl_df.to_csv(CLIP_DB_FILE, index=False, encoding='utf-8-sig')
                    except: pass
                # 2) clip_items 전체를 새로 저장 (기존 짤 포함)
                clips_to_save = [c for c in st.session_state.clip_items if c.get("data")]
                for ci_idx, ci in enumerate(clips_to_save):
                    clip_memo = str(st.session_state.get(f"clip_memo_{ci_idx}", ci.get("memo", ""))).strip()
                    clip_fname = ds
                    if part_in > 0: clip_fname += f"-{part_in}"
                    if clip_memo: clip_fname += f"_{clip_memo}"
                    clip_fname += ci["ext"]
                    clip_path = os.path.join(CLIP_SAVE_DIR, clip_fname)
                    os.makedirs(CLIP_SAVE_DIR, exist_ok=True)
                    with open(clip_path, "wb") as _f:
                        _f.write(ci["data"])
                    clip_row = pd.DataFrame([{
                        "날짜": ds,
                        "N부": str(part_in) if part_in > 0 else "",
                        "파일명": clip_fname,
                        "메모": clip_memo,
                        "방송분류": final_fname
                    }])
                    clip_row.to_csv(CLIP_DB_FILE, mode='a', header=not os.path.exists(CLIP_DB_FILE), index=False, encoding='utf-8-sig')
                st.session_state.clip_items = []
                st.session_state.clip_uploader_key = st.session_state.get('clip_uploader_key', 0) + 1

                reset_form()
                st.session_state.menu_idx = 1
                st.rerun()
            if cancel_clicked or sidebar_cancel:
                reset_form()
                st.session_state.menu_idx = 1
                st.rerun()

    if not st.session_state.form_data["edit_mode"]:
        st.subheader("📋 저장 대기 목록")
        if st.session_state.temp_buffer:
            t_df = pd.DataFrame(st.session_state.temp_buffer)
            disp_cols = [c for c in RENAME_MAP.values() if c in t_df.columns and c != "그림"]
            st.data_editor(t_df[disp_cols], num_rows="dynamic", use_container_width=True, hide_index=True, key="temp_editor")
            if st.button("💾 최종 저장", type="primary", use_container_width=True):
                deleted_rows = st.session_state.temp_editor.get("deleted_rows", [])
                all_details = load_details_db()
                
                for i, row_data in enumerate(st.session_state.temp_buffer):
                    if i in deleted_rows: continue 
                    img_ns = []
                    pt_val = row_data.get("_part_val", 0)
                    for info in row_data.get("_img_data", []):
                        if info.get("empty"):
                            img_ns.append("")
                            continue

                        fn_base = row_data['날짜']
                        if pt_val > 0:
                            fn_base += f"-{pt_val}"
                        
                        body_parts = info.get("body_parts", [])
                        if body_parts:
                            nm = f"{fn_base}_{'_'.join(body_parts)}{info['ext']}"
                        else:
                            nm = f"{fn_base}{info['ext']}"
                            
                        with open(os.path.join(ABS_DIR, nm), "wb") as f:
                            f.write(info["file_data"])
                        img_ns.append(nm)
                    
                    reverse_rename = {v: k for k, v in RENAME_MAP.items()}
                    fr = {reverse_rename.get(k, k): str(v) for k, v in row_data.items() if k not in ["_img_data", "_part_val", "_json_meta"]}
                    fr["이미지파일명"] = ", ".join([n for n in img_ns if n])
                    # CSV_HEADER에 있는 컬럼이 fr에 없으면 빈값으로 채움
                    for col in CSV_HEADER:
                        if col not in fr:
                            fr[col] = ""
                    
                    json_meta = row_data["_json_meta"]
                    if "그림" in json_meta:
                        for idx, entry in enumerate(json_meta["그림"]):
                            if idx < len(img_ns):
                                entry["img_name"] = img_ns[idx]

                    final_df = pd.DataFrame([fr])[CSV_HEADER]
                    final_df.to_csv(DB_FILE, mode='a', header=not os.path.exists(DB_FILE), index=False, encoding='utf-8-sig')
                    all_details[row_data["분류"]] = json_meta
                
                save_details_db(all_details)
                st.session_state.temp_buffer = []
                st.success("저장 완료!")
                st.rerun()

elif menu == "레바실록":
    st.title("📂 레바실록")

    if 'hist_editor_key' not in st.session_state:
        st.session_state.hist_editor_key = 0

    def handle_editor_change():
        editor_state = st.session_state.get("hist_editor", {})
        edits = editor_state.get("edited_rows", {})
        if not edits:
            return
        if not os.path.exists(DB_FILE):
            return

        # 패널 관련 체크박스가 변경된 경우만 처리
        panel_keys = {"_memo_cb", "_src_cb", "_tl_cb"}
        has_panel_change = any(
            panel_keys & set(changes.keys())
            for changes in edits.values()
        )
        if not has_panel_change:
            return  # 선택 체크박스 등 다른 변경은 그냥 통과

        try:
            _df = pd.read_csv(DB_FILE, dtype=str).fillna("")
            _df = _df.sort_values(by="파일명", ascending=False)
            _df["_orig_idx"] = _df.index
            _df = _df.reset_index(drop=True)
        except:
            return

        for row_idx_str, changes in edits.items():
            row_idx = int(row_idx_str)
            if row_idx >= len(_df):
                continue
            trow = _df.iloc[row_idx]
            oidx = int(trow["_orig_idx"])
            cur  = st.session_state.show_memo_info

            if "_memo_cb" in changes:
                if cur and cur.get("index") == oidx and cur.get("type") in ("view", "edit"):
                    st.session_state.show_memo_info = None
                else:
                    st.session_state.show_memo_info = {
                        "index": oidx, "content": str(trow.get("메모", "")),
                        "type": "view", "title": str(trow.get("제목", ""))
                    }
                break
            if "_src_cb" in changes:
                src_path = str(trow.get("소스CSV", ""))
                if src_path and not os.path.isabs(src_path):
                    src_path = os.path.join(SOURCE_SAVE_DIR, src_path)
                if cur and cur.get("index") == oidx and cur.get("type") == "source":
                    st.session_state.show_memo_info = None
                else:
                    st.session_state.show_memo_info = {
                        "index": oidx, "source_csv_path": src_path,
                        "type": "source", "title": str(trow.get("제목", ""))
                    }
                break
            if "_tl_cb" in changes:
                tl_path = _tl_basename(str(trow.get("타임라인CSV", "")))
                if tl_path:
                    tl_path = os.path.join(TIMELINE_SAVE_DIR, tl_path)
                if cur and cur.get("index") == oidx and cur.get("type") == "timeline":
                    st.session_state.show_memo_info = None
                else:
                    st.session_state.show_memo_info = {
                        "index": oidx, "timeline_csv_path": tl_path,
                        "type": "timeline", "title": str(trow.get("제목", ""))
                    }
                break

        # 패널 체크박스 변경분만 edited_rows에서 제거 (선택 등 다른 변경은 보존)
        new_edits = {}
        for row_idx_str, changes in edits.items():
            remaining = {k: v for k, v in changes.items() if k not in panel_keys}
            if remaining:
                new_edits[row_idx_str] = remaining
        if "hist_editor" in st.session_state:
            st.session_state["hist_editor"]["edited_rows"] = new_edits

    if os.path.exists(DB_FILE):
        df_raw = pd.read_csv(DB_FILE, dtype=str).fillna("")
        if "메모" not in df_raw.columns:
            df_raw["메모"] = ""
            
        df_raw = df_raw.sort_values(by="파일명", ascending=False)
        
        if 'part' in df_raw.columns:
            df_raw['part'] = df_raw['part'].apply(lambda x: x.replace('.0', '') if x.endswith('.0') else x)
        df_raw['_orig_idx'] = df_raw.index
        
        df_renamed = df_raw.rename(columns=RENAME_MAP)
        v_df = df_renamed.copy()
        
        # 내부 참조용 원본 메모 저장
        v_df["메모_원본"] = v_df["메모"]

        # [수정 5] 상세 포맷 변환: '주제: 상세내용' → '상세내용만 | 구분' (그림주제 제외)
        def format_detail_display(detail_str):
            if not detail_str or not detail_str.strip():
                return detail_str
            parts = [p.strip() for p in detail_str.split(" | ") if p.strip()]
            result_parts = []
            for p in parts:
                # '그림주제:' 형태는 기존 방식 유지
                if p.startswith("그림"):
                    result_parts.append(p)
                elif ": " in p:
                    # '주제: 상세내용' → '상세내용' 만 추출
                    result_parts.append(p.split(": ", 1)[1])
                else:
                    result_parts.append(p)
            return " | ".join(result_parts)

        v_df["상세"] = v_df["상세"].apply(format_detail_display)
        
        sc1, sc2 = st.columns([8.5, 1.5])
        with sc2:
            if not PUBLIC_MODE and st.button("💾 DB 저장", use_container_width=True):
                df_raw.drop(columns=['_orig_idx'])[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                st.toast("변경사항이 DB에 저장되었습니다.")
        
        st.sidebar.markdown("---")
        if PUBLIC_MODE:
            st.sidebar.caption("📊 아이콘이 있는 방송은 타임라인 파일이 있어요. 타임라인 열의 체크박스를 클릭하면 타임스탬프를 볼 수 있어요.")
        btn1, btn2 = st.sidebar.columns(2)
        skw = st.sidebar.text_input("", placeholder="키워드 , 다중 검색", label_visibility="collapsed", autocomplete="off")
        kws = [k.strip() for k in skw.split(",") if k.strip()]
        
        st.sidebar.caption("🗓️ 기간 필터")
        q_col1, q_col2 = st.sidebar.columns(2)
        years = [str(y) for y in range(datetime.now().year, 2014, -1)]
        months = [f"{m:02d}" for m in range(1, 13)]
        sel_y = q_col1.selectbox("연도", ["전체"] + years, label_visibility="collapsed")
        sel_m = q_col2.selectbox("월", ["전체"] + months, label_visibility="collapsed")
        
        c_d1, c_d2 = st.sidebar.columns(2)
        if sel_y != "전체":
            q_start = datetime(int(sel_y), int(sel_m) if sel_m != "전체" else 1, 1)
            q_end = (q_start + timedelta(days=32)).replace(day=1) - timedelta(days=1) if sel_m != "전체" else datetime(int(sel_y), 12, 31)
            def_start, def_end = q_start.date(), q_end.date()
        else:
            def_start, def_end = datetime(2015, 1, 1).date(), (datetime.now() + timedelta(days=365)).date()
        start_d = c_d1.date_input("시작", value=def_start, label_visibility="collapsed")
        end_d = c_d2.date_input("종료", value=def_end, label_visibility="collapsed")
        
        with st.sidebar.expander("📌 상세 필터", expanded=False):
            fg = st.radio("손님", ["전체", "있음", "없음"], horizontal=True)
            fv = st.radio("영도", ["전체", "있음", "없음"], horizontal=True)
            if not PUBLIC_MODE:
                fs = st.radio("주식", ["전체", "있음", "없음"], horizontal=True)
            else:
                fs = "전체"
        
        if not PUBLIC_MODE:
            st.sidebar.markdown("---")
            st.sidebar.caption("🔗 STT 연동")
            if st.sidebar.button("🔗 STT CSV 자동 매칭", use_container_width=True):
                df_for_match = pd.read_csv(DB_FILE, dtype=str).fillna("")
                df_for_match, cnt = auto_match_stt(df_for_match)
                if cnt > 0:
                    df_for_match[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                    st.sidebar.success(f"{cnt}개 방송에 타임라인 CSV 연결 완료!")
                    st.rerun()
                else:
                    st.sidebar.info("새로 매칭할 항목이 없습니다.")

            if st.sidebar.button("🔧 파일명 일괄 정규화", use_container_width=True):
                df_norm = pd.read_csv(DB_FILE, dtype=str).fillna("")
                df_norm, fix_cnt, fix_details, norm_session = normalize_filenames(df_norm)
                if fix_cnt > 0:
                    df_norm[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                    st.session_state["last_norm_session"] = norm_session
                    st.sidebar.success(f"{fix_cnt}건 수정 완료 (세션: {norm_session})")
                    with st.sidebar.expander("수정 내역 보기"):
                        for d in fix_details:
                            st.caption(d)
                    st.rerun()
                else:
                    st.sidebar.info("수정할 항목이 없습니다.")

            _norm_sessions = []
            if os.path.exists(NORMALIZE_LOG):
                import json as _j
                _seen = {}
                with open(NORMALIZE_LOG, encoding="utf-8") as _lf:
                    for _line in _lf:
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _e = _j.loads(_line)
                            _sid = _e.get("session", "")
                            if _sid and _sid not in _seen:
                                _seen[_sid] = _e.get("at", _sid)
                                _norm_sessions.append(_sid)
                        except Exception:
                            pass

            if _norm_sessions:
                with st.sidebar.expander("↩️ 정규화 되돌리기"):
                    _sel = st.selectbox(
                        "되돌릴 세션 선택",
                        options=list(reversed(_norm_sessions)),
                        format_func=lambda s: s.replace("_", " "),
                        key="revert_session_select",
                    )
                    if st.button("선택한 세션 원복", use_container_width=True, key="revert_norm_btn"):
                        rev_cnt, rev_errs = revert_normalize_session(_sel)
                        if rev_cnt > 0:
                            st.sidebar.success(f"{rev_cnt}건 원복 완료")
                        if rev_errs:
                            for _e in rev_errs:
                                st.sidebar.warning(_e)
                        elif rev_cnt == 0:
                            st.sidebar.info("원복할 파일 변경 내역이 없습니다.")
                        st.rerun()

            st.sidebar.markdown("---")
            st.sidebar.caption("📦 백업 및 복구")
            if st.sidebar.button("🔄 방송정보 재동기화", use_container_width=True):
                auto_sync_live_info()
                st.sidebar.success("동기화 완료!")
                st.rerun()

            if st.sidebar.button("📄 백업 파일 생성", use_container_width=True):
                if not os.path.exists(BACKUP_DIR):
                    os.makedirs(BACKUP_DIR, exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                b_csv_name = f"backup_{timestamp}.csv"
                df_raw.drop(columns=['_orig_idx'])[CSV_HEADER].to_csv(os.path.join(BACKUP_DIR, b_csv_name), index=False, encoding='utf-8-sig')
                b_json_name = f"backup_{timestamp}.json"
                all_details = load_details_db()
                with open(os.path.join(BACKUP_DIR, b_json_name), 'w', encoding='utf-8') as f:
                    json.dump(all_details, f, ensure_ascii=False, indent=4)
                st.sidebar.success(f"백업 완료 ({timestamp})")

            if os.path.exists(BACKUP_DIR):
                b_files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.csv')], reverse=True)
                if b_files:
                    sel_b = st.sidebar.selectbox("백업 선택", b_files, label_visibility="collapsed")
                    if st.sidebar.button("🔄 백업본 가져오기", use_container_width=True):
                        b_path = os.path.join(BACKUP_DIR, sel_b)
                        pd.read_csv(b_path, dtype=str)[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                        json_b_path = b_path.replace(".csv", ".json")
                        if os.path.exists(json_b_path):
                            with open(json_b_path, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                        save_details_db(data)
                        st.sidebar.success("복구 완료!")
                        st.rerun()

        if kws:
            v_df = v_df[v_df.apply(lambda r: any(k.lower() in str(v).lower() for k in kws for v in r), axis=1)]
        v_df['dt_t'] = pd.to_datetime(v_df['날짜'], format='%Y%m%d', errors='coerce')
        v_df = v_df[(v_df['dt_t'].dt.date >= start_d) & (v_df['dt_t'].dt.date <= end_d)]
        
        if fg == "있음": v_df = v_df[v_df['손님'] != ""]
        elif fg == "없음": v_df = v_df[v_df['손님'] == ""]
        if fv == "있음": v_df = v_df[v_df['영도'] == "O"]
        elif fv == "없음": v_df = v_df[v_df['영도'] == ""]
        if fs == "있음": v_df = v_df[v_df['주식'] == "O"]
        elif fs == "없음": v_df = v_df[v_df['주식'] == ""]
        
        if "선택" not in v_df.columns:
            v_df.insert(0, "선택", False)
        # 완료 → 아이콘으로 변환 (체크박스 대신 텍스트)
        v_df["완료"] = v_df["완료"].apply(lambda x: "✅" if str(x).lower() == 'true' else "")

        # 메모/타임라인 존재 여부 → 아이콘 텍스트 컬럼으로 표시
        def memo_icon(val):
            v = str(val).strip()
            if v == "" or v.lower() == "false" or v.lower() == "nan":
                return ""
            return "📝"

        def timeline_icon(val):
            v = str(val).strip()
            if v == "" or v.lower() == "false" or v.lower() == "nan":
                return ""
            return "📊"

        # 메모/소스/타임라인 존재 여부 → 아이콘 텍스트 컬럼 + 체크박스
        def has_val(v):
            s = str(v).strip()
            return s and s.lower() not in ("", "false", "nan")

        v_df["메모아이콘"]     = v_df["메모_원본"].apply(lambda v: "📝" if has_val(v) else "")
        v_df["소스아이콘"]     = v_df["소스CSV"].apply(lambda v: "🎬" if has_val(v) else "") if "소스CSV" in v_df.columns else ""
        v_df["타임라인아이콘"] = v_df["타임라인CSV"].apply(lambda v: "📊" if has_val(v) else "") if "타임라인CSV" in v_df.columns else ""

        # 체크박스 초기값: 현재 열린 패널에 해당하는 행/타입만 True
        cur_info = st.session_state.show_memo_info
        def _cb_val(row, cb_type):
            if cur_info is None: return False
            oidx = int(row["_orig_idx"])
            if cur_info.get("index") != oidx: return False
            t = cur_info.get("type","")
            if cb_type == "memo"   and t in ("view","edit"): return True
            if cb_type == "src"    and t == "source":        return True
            if cb_type == "tl"     and t == "timeline":      return True
            return False

        v_df["_memo_cb"] = v_df.apply(lambda r: _cb_val(r, "memo"), axis=1)
        v_df["_src_cb"]  = v_df.apply(lambda r: _cb_val(r, "src"),  axis=1)
        v_df["_tl_cb"]   = v_df.apply(lambda r: _cb_val(r, "tl"),   axis=1)

        # ── 패널 열림 여부로 사이드바 숨김 CSS 제어 ─────────────
        info = st.session_state.show_memo_info
        panel_open = info is not None

        if panel_open:
            st.markdown("""<style>[data-testid="stSidebar"]{display:none}</style>""", unsafe_allow_html=True)
            col_table, col_panel = st.columns([5, 5])
        else:
            col_table = st.container()
            col_panel = None

        with col_table:
            _PUBLIC_HIDDEN = {"선택", "완료", "분류", "메모아이콘", "_memo_cb", "소스아이콘", "_src_cb", "주식"}
            display_cols = ["선택", "완료", "제목", "분류", "날짜", "N부", "시간", "길이", "주제", "상세", "다시보기",
                            "메모아이콘", "_memo_cb", "소스아이콘", "_src_cb", "타임라인아이콘", "_tl_cb",
                            "손님", "영도", "주식"]
            if PUBLIC_MODE:
                display_cols = [c for c in display_cols if c not in _PUBLIC_HIDDEN]
            display_df = v_df[[c for c in display_cols if c in v_df.columns]]

            column_config = {
                "선택":           st.column_config.CheckboxColumn(width="small", disabled=False),
                "완료":           st.column_config.TextColumn("완료", width="small", disabled=True),
                "제목":           st.column_config.TextColumn(disabled=True),
                "분류":           st.column_config.TextColumn(disabled=True),
                "날짜":           st.column_config.TextColumn(disabled=True),
                "N부":            st.column_config.TextColumn(disabled=True),
                "시간":           st.column_config.TextColumn(width="small", disabled=True),
                "길이":           st.column_config.TextColumn(disabled=True),
                "주제":           st.column_config.TextColumn(disabled=True),
                "상세":           st.column_config.TextColumn(width="medium", disabled=True),
                "다시보기":       st.column_config.LinkColumn(disabled=True),
                "메모아이콘":     st.column_config.TextColumn("📝", width="small", disabled=True),
                "_memo_cb":       st.column_config.CheckboxColumn("메모", width="small", disabled=False),
                "소스아이콘":     st.column_config.TextColumn("🎬", width="small", disabled=True),
                "_src_cb":        st.column_config.CheckboxColumn("소스", width="small", disabled=False),
                "타임라인아이콘": st.column_config.TextColumn("📊", width="small", disabled=True),
                "_tl_cb":         st.column_config.CheckboxColumn("타임라인", width="small", disabled=False),
                "손님":           st.column_config.TextColumn(disabled=True),
                "영도":           st.column_config.TextColumn(disabled=True),
                "주식":           st.column_config.TextColumn(disabled=True),
            }

            ed_h = st.data_editor(
                display_df,
                use_container_width=True,
                hide_index=True,
                key="hist_editor",
                column_config=column_config,
                on_change=handle_editor_change,
                height=720,
            )

        # ── 우측 패널 (5:5) ────────────────────────────────────────
        if panel_open and col_panel is not None:
            with col_panel:
                with st.container(border=True):
                    row_title = info.get("title", "")

                    if info["type"] in ("view", "edit"):
                        st.markdown("### 📝 메모")
                        if row_title: st.caption(row_title)
                        st.divider()
                        if info["type"] == "view":
                            content = str(info.get("content", ""))
                            st.markdown(content if content else "_메모 없음_")
                            st.divider()
                            pc1, pc2 = st.columns(2)
                            if not PUBLIC_MODE:
                                if pc1.button("✏️ 수정하기", use_container_width=True, key="panel_to_edit"):
                                    st.session_state.show_memo_info = {**info, "type": "edit"}
                                    st.rerun()
                            if (pc2 if not PUBLIC_MODE else pc1).button("✕ 닫기", use_container_width=True, key="panel_close_view"):
                                st.session_state.show_memo_info = None
                                st.rerun()
                        else:
                            new_memo = st.text_area("메모", value=str(info.get("content","")),
                                                    height=420, key="panel_memo_area", label_visibility="collapsed")
                            pc1, pc2, pc3 = st.columns(3)
                            if pc1.button("💾 저장", use_container_width=True, type="primary", key="panel_save"):
                                if os.path.exists(DB_FILE):
                                    full_df = pd.read_csv(DB_FILE, dtype=str).fillna("")
                                    if "메모" not in full_df.columns: full_df["메모"] = ""
                                    full_df.at[info["index"], "메모"] = str(new_memo)
                                    full_df[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                                    st.toast("✅ 메모 저장 완료")
                                    st.session_state.show_memo_info = None
                                    st.rerun()
                            if pc2.button("👁 보기", use_container_width=True, key="panel_to_view"):
                                st.session_state.show_memo_info = {**info, "type":"view", "content": new_memo}
                                st.rerun()
                            if pc3.button("✕ 닫기", use_container_width=True, key="panel_close_edit"):
                                st.session_state.show_memo_info = None
                                st.rerun()

                    elif info["type"] == "source":
                        st.markdown("### 🎬 소스")
                        if row_title: st.caption(row_title)
                        st.divider()
                        src_path = info.get("source_csv_path", "")
                        if src_path and os.path.exists(src_path):
                            st.dataframe(pd.read_csv(src_path), use_container_width=True, height=500)
                            if st.button("🗑️ 소스 파일 삭제", key="del_src_panel"):
                                os.remove(src_path)
                                full_df = pd.read_csv(DB_FILE, dtype=str).fillna("")
                                full_df.at[info["index"], "소스CSV"] = ""
                                full_df[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                                st.session_state.show_memo_info = None
                                st.toast("소스 파일이 삭제되었습니다.")
                                st.rerun()
                        else:
                            st.warning("소스 CSV 파일을 찾을 수 없습니다.")
                            if src_path: st.caption(f"경로: {src_path}")
                        if st.button("✕ 닫기", use_container_width=True, key="panel_close_src"):
                            st.session_state.show_memo_info = None
                            st.rerun()

                    elif info["type"] == "timeline":
                        st.markdown("### 📊 타임라인")
                        if row_title: st.caption(row_title)
                        st.divider()
                        tl_path = info.get("timeline_csv_path", "")
                        if tl_path and os.path.exists(tl_path):
                            st.dataframe(pd.read_csv(tl_path), use_container_width=True, height=500)
                            if st.button("🗑️ 타임라인 파일 삭제", key="del_tl_panel"):
                                os.remove(tl_path)
                                full_df = pd.read_csv(DB_FILE, dtype=str).fillna("")
                                full_df.at[info["index"], "타임라인CSV"] = ""
                                full_df[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
                                st.session_state.show_memo_info = None
                                st.toast("타임라인 파일이 삭제되었습니다.")
                                st.rerun()
                        else:
                            st.warning("타임라인 CSV 파일을 찾을 수 없습니다.")
                            if tl_path: st.caption(f"경로: {tl_path}")
                        if st.button("✕ 닫기", use_container_width=True, key="panel_close_tl"):
                            st.session_state.show_memo_info = None
                            st.rerun()

        sel_for_btn = ed_h[ed_h["선택"] == True] if "선택" in ed_h.columns else ed_h.iloc[0:0]

        if not PUBLIC_MODE and btn1.button("📝 수정", use_container_width=True, disabled=len(sel_for_btn)!=1):
            r = sel_for_btn.iloc[0]
            # ed_h의 인덱스로 v_df에서 대응되는 행을 찾아 orig_idx 추출
            actual_idx = int(v_df.loc[sel_for_btn.index[0], '_orig_idx'])
            file_id = r['분류']
            
            all_meta = load_details_db()
            saved_meta = all_meta.get(file_id, {})
            
            topics_list = [t.strip() for t in r['주제'].split(",") if t.strip()]
            
            st.session_state.drawing_count = len(saved_meta.get("그림", [{}]))
            for cat in topics_list:
                if cat != "그림":
                    st.session_state[f"c_{cat}"] = len(saved_meta.get(cat, [{}]))

            st.session_state.form_data = {
                "date": datetime.strptime(r['날짜'], '%Y%m%d').date(),
                "part": int(r['N부']) if r['N부']!="" else 0,
                "start_t": r['시간'],
                "dur": r['길이'],
                "title": r['제목'],
                "url": r['다시보기'],
                "timeline_csv": str(v_df.loc[sel_for_btn.index[0], '타임라인CSV']) if '타임라인CSV' in v_df.columns else "",
                "source_csv": str(v_df.loc[sel_for_btn.index[0], '소스CSV']) if '소스CSV' in v_df.columns else "",
                "topics": topics_list,
                "details": r['상세'],
                "memo": str(v_df.loc[sel_for_btn.index[0], '메모_원본']),
                "guest": [g.strip() for g in r['손님'].split(",") if g.strip()],
                "v_tube": r['영도'] == "O",
                "stock": r['주식'] == "O",
                "is_done": r['완료'] == "✅",
                "parsed_details": saved_meta,
                "edit_mode": True,
                "edit_index": actual_idx
            }
            # 해당 방송의 기존 짤을 clip_log에서 불러와 clip_items에 세팅
            existing_clips = []
            if os.path.exists(CLIP_DB_FILE):
                try:
                    cl_df = pd.read_csv(CLIP_DB_FILE, dtype=str).fillna("")
                    matched = cl_df[cl_df["방송분류"] == file_id]
                    for _, cl_row in matched.iterrows():
                        fn = str(cl_row["파일명"]).strip()
                        clip_path = os.path.join(CLIP_SAVE_DIR, fn)
                        if fn and os.path.exists(clip_path):
                            with open(clip_path, "rb") as _f:
                                clip_data = _f.read()
                            existing_clips.append({
                                "orig_name": fn,
                                "data": clip_data,
                                "ext": os.path.splitext(fn)[1].lower(),
                                "memo": str(cl_row.get("메모", ""))
                            })
                except: pass
            st.session_state.clip_items = existing_clips
            st.session_state.clip_uploader_key = st.session_state.get('clip_uploader_key', 0) + 1
            st.session_state.menu_idx = 0
            st.rerun()

        if not PUBLIC_MODE and btn2.button("🗑️ 삭제", use_container_width=True, disabled=len(sel_for_btn)==0):
            target_fnames = v_df.loc[sel_for_btn.index, '분류'].tolist()
            df_raw.drop(v_df.loc[sel_for_btn.index, '_orig_idx'].tolist()).drop(columns=['_orig_idx'])[CSV_HEADER].to_csv(DB_FILE, index=False, encoding='utf-8-sig')
            all_details = load_details_db()
            for fn in target_fnames:
                if fn in all_details: del all_details[fn]
            save_details_db(all_details)
            st.rerun()

elif menu == "레바 그림 갤러리":
    st.markdown('<div class="gallery-header"><h1>🖼️ 레바 그림 갤러리</h1></div>', unsafe_allow_html=True)
    g_skw = st.sidebar.text_input("검색", placeholder="키워드 , 다중 검색", label_visibility="collapsed", autocomplete="off")
    g_kws = [k.strip() for k in g_skw.split(",") if k.strip()]
    st.sidebar.caption("🗓️ 기간 필터")
    gq_col1, gq_col2 = st.sidebar.columns(2)
    g_sel_y = gq_col1.selectbox("연도", ["전체"] + [str(y) for y in range(datetime.now().year, 2014, -1)], key="g_sy")
    g_sel_m = gq_col2.selectbox("월", ["전체"] + [f"{m:02d}" for m in range(1, 13)], key="g_sm")
    c_gd1, c_gd2 = st.sidebar.columns(2)
    if g_sel_y != "전체":
        gq_start = datetime(int(g_sel_y), int(g_sel_m) if g_sel_m != "전체" else 1, 1)
        gq_end = (gq_start + timedelta(days=32)).replace(day=1) - timedelta(days=1) if g_sel_m != "전체" else datetime(int(g_sel_y), 12, 31)
        gdef_start, gdef_end = gq_start.date(), gq_end.date()
    else:
        gdef_start, gdef_end = datetime(2015, 1, 1).date(), (datetime.now() + timedelta(days=365)).date()
    g_start_d = c_gd1.date_input("시작", value=gdef_start, key="g_sd")
    g_end_d = c_gd2.date_input("종료", value=gdef_end, key="g_ed")

    if os.path.exists(DB_FILE):
        df_g = pd.read_csv(DB_FILE, dtype=str).fillna("")
        df_g = df_g[df_g["이미지파일명"] != ""]
        df_g['dt_t'] = pd.to_datetime(df_g['날짜'], format='%Y%m%d', errors='coerce')
        df_g = df_g[(df_g['dt_t'].dt.date >= g_start_d) & (df_g['dt_t'].dt.date <= g_end_d)]

        if not df_g.empty:
            all_imgs = []
            for _, row in df_g.iterrows():
                for img_nm in [i.strip() for i in str(row["이미지파일명"]).split(",") if i.strip()]:
                    if not g_kws or any(k.lower() in img_nm.lower() or k.lower() in str(row["제목"]).lower() for k in g_kws):
                        all_imgs.append({"name": img_nm})

            all_imgs.sort(key=lambda x: x["name"], reverse=True)

            import base64, mimetypes

            def _img_html(path, blur_px=0, width="100%"):
                mime = mimetypes.guess_type(path)[0] or "image/png"
                with open(path, "rb") as _f:
                    b64 = base64.b64encode(_f.read()).decode()
                blur_style = f"filter:blur({blur_px}px);" if blur_px else ""
                return (
                    f'<img src="data:{mime};base64,{b64}" '
                    f'style="width:{width};border-radius:6px;{blur_style}">'
                )

            @st.dialog("🖼️ 그림 보기", width="large")
            def _show_gallery_dialog(p):
                blur = 4 if PUBLIC_MODE else 0
                st.markdown(_img_html(p, blur_px=blur, width="100%"), unsafe_allow_html=True)

            cols = st.columns(4)
            for idx, img in enumerate(all_imgs):
                path = os.path.join(ABS_DIR, img["name"])
                if os.path.exists(path):
                    with cols[idx % 4]:
                        blur = 4 if PUBLIC_MODE else 0
                        st.markdown(_img_html(path, blur_px=blur), unsafe_allow_html=True)
                        if st.button("전체보기", key=f"gview_{idx}", use_container_width=True):
                            _show_gallery_dialog(path)
                        name_no_ext = os.path.splitext(img["name"])[0]
                        st.markdown(f"<p style='font-size:0.85rem;'>{name_no_ext}</p>", unsafe_allow_html=True)

elif menu == "레바 짤 갤러리":
    st.markdown('<div class="gallery-header"><h1>📸 레바 짤 갤러리</h1></div>', unsafe_allow_html=True)
    cg_skw = st.sidebar.text_input("검색", placeholder="키워드 , 다중 검색", label_visibility="collapsed", autocomplete="off")
    cg_kws = [k.strip() for k in cg_skw.split(",") if k.strip()]
    st.sidebar.caption("🗓️ 기간 필터")
    cgq_col1, cgq_col2 = st.sidebar.columns(2)
    cg_sel_y = cgq_col1.selectbox("연도", ["전체"] + [str(y) for y in range(datetime.now().year, 2014, -1)], key="cg_sy")
    cg_sel_m = cgq_col2.selectbox("월", ["전체"] + [f"{m:02d}" for m in range(1, 13)], key="cg_sm")
    cgd1, cgd2 = st.sidebar.columns(2)
    if cg_sel_y != "전체":
        cgq_start = datetime(int(cg_sel_y), int(cg_sel_m) if cg_sel_m != "전체" else 1, 1)
        cgq_end = (cgq_start + timedelta(days=32)).replace(day=1) - timedelta(days=1) if cg_sel_m != "전체" else datetime(int(cg_sel_y), 12, 31)
        cgdef_start, cgdef_end = cgq_start.date(), cgq_end.date()
    else:
        cgdef_start, cgdef_end = datetime(2015, 1, 1).date(), (datetime.now() + timedelta(days=365)).date()
    cg_start_d = cgd1.date_input("시작", value=cgdef_start, key="cg_sd")
    cg_end_d   = cgd2.date_input("종료", value=cgdef_end,   key="cg_ed")

    if os.path.exists(CLIP_DB_FILE):
        df_cl = pd.read_csv(CLIP_DB_FILE, dtype=str).fillna("")
        df_cl['dt_t'] = pd.to_datetime(df_cl['날짜'], format='%Y%m%d', errors='coerce')
        df_cl = df_cl[(df_cl['dt_t'].dt.date >= cg_start_d) & (df_cl['dt_t'].dt.date <= cg_end_d)]

        if not df_cl.empty:
            all_clips = []
            for _, row in df_cl.iterrows():
                fn = str(row["파일명"]).strip()
                if not fn: continue
                if not cg_kws or any(k.lower() in fn.lower() or k.lower() in str(row.get("메모","")).lower() for k in cg_kws):
                    all_clips.append({"name": fn})

            all_clips.sort(key=lambda x: x["name"], reverse=True)
            if all_clips:
                @st.dialog("📸 짤 보기", width="large")
                def _show_clip_dialog(p):
                    st.image(p, use_container_width=True)

                cols = st.columns(4)
                shown = 0
                for idx, clip in enumerate(all_clips):
                    clip_path = os.path.join(CLIP_SAVE_DIR, clip["name"])
                    if not os.path.isabs(CLIP_SAVE_DIR):
                        clip_path = os.path.abspath(clip_path)
                    if os.path.exists(clip_path):
                        with cols[shown % 4]:
                            import base64, mimetypes
                            mime = mimetypes.guess_type(clip_path)[0] or "image/png"
                            with open(clip_path, "rb") as _f:
                                b64 = base64.b64encode(_f.read()).decode()
                            st.markdown(
                                f'<img src="data:{mime};base64,{b64}" style="width:100%;border-radius:6px;">',
                                unsafe_allow_html=True
                            )
                            if st.button("전체보기", key=f"cgview_{idx}", use_container_width=True):
                                _show_clip_dialog(clip_path)
                            name_no_ext = os.path.splitext(clip["name"])[0]
                            st.markdown(f"<p style='font-size:0.85rem;'>{name_no_ext}</p>", unsafe_allow_html=True)
                        shown += 1
                if shown == 0:
                    st.info(f"저장된 짤 파일을 찾을 수 없습니다. 저장 폴더를 확인해 주세요.\n\n저장 경로: `{CLIP_SAVE_DIR}`")
            else:
                st.info("검색 결과가 없습니다.")
        else:
            st.info("해당 기간에 저장된 레바 짤이 없습니다.")
    else:
        st.info("아직 저장된 레바 짤이 없습니다. 입력 메뉴에서 짤을 추가해 주세요.")

elif menu in ("소스 검색", "타임라인 검색"):
    is_source = menu == "소스 검색"
    folder_dir = SOURCE_SAVE_DIR if is_source else TIMELINE_SAVE_DIR
    page_icon = "🎬" if is_source else "📊"
    st.title(f"{page_icon} {menu}")

    if rfuzz is None:
        st.error("`rapidfuzz` 패키지가 필요합니다: 터미널에서 `pip install rapidfuzz` 실행 후 앱을 재시작해 주세요.")
        st.stop()

    col_kw, col_fuzzy = st.columns([5, 2])
    kw_input = col_kw.text_input(
        "", placeholder="키워드 , 다중 검색 (쉼표 구분)",
        label_visibility="collapsed", key=f"{menu}_kw", autocomplete="off"
    )
    use_fuzzy = col_fuzzy.checkbox("유사 검색 (STT 오타 보정)", value=False, key=f"{menu}_fuzzy")

    if use_fuzzy:
        threshold = st.slider(
            "유사도 임계값 (%)", 30, 100, 75, 5, key=f"{menu}_thresh",
            help="자모 분리 기반 비교 — 낮을수록 더 관대하게 매칭. 70~80 권장 (로캣↔로켓 약 80%)"
        )
    else:
        threshold = 80

    keywords = [k.strip() for k in kw_input.split(",") if k.strip()]

    if not keywords:
        st.info("검색어를 입력하세요.")
    elif not os.path.exists(folder_dir):
        st.warning(f"폴더를 찾을 수 없습니다: `{folder_dir}`")
    else:
        with st.spinner("검색 중..."):
            results = search_csv_files(folder_dir, keywords, use_fuzzy, threshold)

        total_rows = sum(len(r["rows"]) for r in results)
        if results:
            summary_col, all_col = st.columns([4, 1])
            summary_col.success(f"**{len(results)}개 파일**, **{total_rows}개 행**에서 발견")
            show_all = all_col.button("전체보기", key=f"{menu}_show_all", use_container_width=True)

            if show_all:
                # 모든 결과를 파일명 열 포함해서 단일 테이블로
                all_rows = []
                for r in results:
                    for row in r["rows"]:
                        all_rows.append({"파일명": r["file"], **row})
                all_df = pd.DataFrame(all_rows)
                # 파일명 열을 앞으로
                cols = ["파일명"] + [c for c in all_df.columns if c != "파일명"]
                st.dataframe(all_df[cols], use_container_width=True, hide_index=True)
            else:
                for r in results:
                    date_str = ""
                    m = re.match(r"(\d{8})", r["file"])
                    if m:
                        try:
                            date_str = f"  {datetime.strptime(m.group(1), '%Y%m%d').strftime('%Y-%m-%d')}"
                        except Exception:
                            pass
                    label = f"{page_icon} {r['file']}{date_str}  —  {len(r['rows'])}건"
                    with st.expander(label, expanded=(len(results) == 1)):
                        df_r = pd.DataFrame(r["rows"])
                        st.dataframe(df_r, use_container_width=True, hide_index=True)
        else:
            st.info("검색 결과가 없습니다.")

elif menu == "카테고리":
    st.title("⚙️ 카테고리 설정")
    config = load_config()
    n_cat = st.text_area("📋 방송 주제 통합 관리", value="\n".join(config.get("categories", [])), height=500, help="주제 뒤에 @를 붙여 세부 항목 추가 가능")
    n_dr = st.text_input("🎨 그림 분류 설정", value=", ".join(config.get("drawing_types", [])), autocomplete="off")
    if st.button("💾 설정 저장", type="primary", use_container_width=True):
        new_config = {
            "categories": [x.strip() for x in n_cat.split("\n") if x.strip()],
            "drawing_types": [x.strip() for x in n_dr.split(",") if x.strip()]
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8-sig') as f:
            json.dump(new_config, f, ensure_ascii=False, indent=4)
        st.success("카테고리 설정이 저장되었습니다.")
        st.rerun()