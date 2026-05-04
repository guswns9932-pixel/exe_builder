import os
import sys
import re
import ast
import locale
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path

DEFAULT_PYINSTALLER_CMD = "pyinstaller"   # "python -m PyInstaller" 형태도 지원
UPX_PATH = ""                              # PATH에 있으면 빈 문자열 가능

# ※ distutils / setuptools / pkg_resources 는 PyInstaller 6.x 내부 훅과 충돌하므로 제외 불가
DEFAULT_EXCLUDES = [
    "tkinter.test", "test", "unittest",
    "doctest", "pdb", "profile", "cProfile", "timeit",
    "lib2to3", "difflib", "pydoc",
]

HEAVY_MODULE_HINTS = {
    "numpy", "pandas", "matplotlib", "scipy", "sklearn",
    "tensorflow", "torch", "tkinter", "PyQt5", "PySide6",
    "opencv", "cv2",
}

# 업데이트 가능 구조용 런처 템플릿
_LAUNCHER_TEMPLATE = """\
# Auto-generated launcher — DO NOT EDIT
import sys as _sys
import os as _os
import runpy as _runpy

if getattr(_sys, 'frozen', False):
    _base = _os.path.dirname(_os.path.abspath(_sys.executable))
else:
    _base = _os.path.dirname(_os.path.abspath(__file__))

_scripts = _os.path.join(_base, 'scripts')
if _scripts not in _sys.path:
    _sys.path.insert(0, _scripts)

# PyInstaller 의존성 감지용 명시적 import
{dep_imports}
_runpy.run_path(_os.path.join(_scripts, {main_script!r}), run_name='__main__')
"""

# 네트워크 드라이브 배포용 BAT 런처 템플릿
# 네트워크 → %LOCALAPPDATA% 동기화 후 로컬에서 실행
# (Windows 보안 정책: 네트워크 경로에서 DLL 로드 차단 우회)
_BAT_TEMPLATE = """\
@echo off
setlocal
chcp 65001 >nul 2>&1

set APP_NAME={app_name}
set NET_BASE=%~dp0
set LOCAL_BASE=%LOCALAPPDATA%\\%APP_NAME%
set PAUSE_NEEDED=0

:: ── 최초 설치 (로컬에 EXE 없음) ─────────────────────────────────────
if not exist "%LOCAL_BASE%\\%APP_NAME%.exe" goto :full_install

:: ── 버전 비교 (scripts 업데이트 감지) ───────────────────────────────
if not exist "%NET_BASE%version.txt" goto :run
if not exist "%LOCAL_BASE%\\version.txt" goto :update_scripts
fc /b "%NET_BASE%version.txt" "%LOCAL_BASE%\\version.txt" >nul 2>&1
if errorlevel 1 goto :update_scripts
goto :run

:: ── 최초 설치 ────────────────────────────────────────────────────────
:full_install
set PAUSE_NEEDED=1
echo ============================================================
echo  [첫 설치] %APP_NAME%
echo ============================================================
echo 잠시 기다려 주세요...
echo.
if not exist "%LOCAL_BASE%" mkdir "%LOCAL_BASE%"
xcopy /E /I /Q /Y "%NET_BASE%%APP_NAME%.exe" "%LOCAL_BASE%\\"
xcopy /E /I /Q /Y "%NET_BASE%_internal\\" "%LOCAL_BASE%\\_internal\\"
if exist "%NET_BASE%scripts\\" (
    xcopy /E /I /Q /Y "%NET_BASE%scripts\\" "%LOCAL_BASE%\\scripts\\"
)
if exist "%NET_BASE%version.txt" (
    copy /Y "%NET_BASE%version.txt" "%LOCAL_BASE%\\version.txt" >nul
)
echo.
echo [설치 완료]
echo 로컬 경로: %LOCAL_BASE%
echo.
explorer "%LOCAL_BASE%"
goto :run

:: ── scripts 업데이트만 적용 ──────────────────────────────────────────
:update_scripts
set PAUSE_NEEDED=1
echo ============================================================
echo  [업데이트] %APP_NAME%
echo ============================================================
if exist "%NET_BASE%scripts\\" (
    xcopy /E /I /Q /Y "%NET_BASE%scripts\\" "%LOCAL_BASE%\\scripts\\"
)
copy /Y "%NET_BASE%version.txt" "%LOCAL_BASE%\\version.txt" >nul
echo [업데이트 완료]
echo.

:: ── 로컬에서 실행 ────────────────────────────────────────────────────
:run
start "" "%LOCAL_BASE%\\%APP_NAME%.exe"
if "%PAUSE_NEEDED%"=="1" (
    echo 앱을 시작했습니다.
    echo 이 창은 아무 키나 누르면 닫힙니다.
    pause >nul
)
endlocal
"""


def get_default_output_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_multiline_list(text: str):
    if not text:
        return []
    items = []
    for x in text.replace(",", "\n").replace(";", "\n").splitlines():
        x = x.strip()
        if x:
            items.append(x)
    seen, out = set(), []
    for i in items:
        if i not in seen:
            out.append(i)
            seen.add(i)
    return out


def safe_read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
        for enc in ("utf-8", "cp949", "mbcs", "latin-1"):
            try:
                return data.decode(enc, errors="replace")
            except Exception:
                pass
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def collect_imports(py_path: str) -> set[str]:
    """py 파일에서 top-level import 이름 수집."""
    try:
        src = safe_read_text(Path(py_path))
        tree = ast.parse(src, filename=py_path)
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mods.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mods.add(node.module.split(".")[0])
        return mods
    except Exception:
        return set()


def extract_hidden_import_candidates(text: str):
    cands = set()
    for m in re.findall(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", text):
        cands.add(m.strip())
    for m in re.findall(r"ImportError:\s+No module named\s+([A-Za-z0-9_\.]+)", text):
        cands.add(m.strip())
    for m in re.findall(r"missing module named ['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE):
        cands.add(m.strip())
    for m in re.findall(r"Hidden import ['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE):
        cands.add(m.strip())
    cleaned = set()
    for x in cands:
        x = x.strip().strip(".")
        if x and " " not in x and len(x) < 200:
            cleaned.add(x)
    return sorted(cleaned)


class ExeBuilderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Py → EXE 자동 변환기")
        self.geometry("980x800")
        self.minsize(980, 800)
        self.resizable(True, True)

        self.py_files: list[str] = []

        self.exe_name = tk.StringVar()
        self.output_dir = tk.StringVar(value=get_default_output_dir())
        self.build_mode = tk.StringVar(value="onedir")   # 기본값: 폴더 모드 (빠른 실행)

        self.enable_advanced = tk.BooleanVar(value=False)
        self.use_upx = tk.BooleanVar(value=False)
        self.noconsole = tk.BooleanVar(value=True)
        self.opt_level = tk.StringVar(value="1")
        self.use_strip = tk.BooleanVar(value=False)
        self.updatable_mode = tk.BooleanVar(value=False)  # 업데이트 가능 구조

        self.enable_runtime_tmpdir = tk.BooleanVar(value=False)
        self.runtime_tmpdir = tk.StringVar(value="")

        self.status_text = tk.StringVar(value="대기 중")

        self.import_modules = []
        self.import_listbox = None
        self.reco_listbox = None

        self.last_dist_dir = None
        self.last_app_name = None
        self.last_exe_path = None

        self._log_queue = queue.Queue()

        self._build_ui_grid()
        self._drain_log()

    # ------------------------------------------------------------------
    # UI 구성
    # ------------------------------------------------------------------
    def _build_ui_grid(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # row0: 파일 목록 + 출력 설정
        frame_file = tk.Frame(self, padx=10, pady=10)
        frame_file.grid(row=0, column=0, sticky="ew")
        frame_file.grid_columnconfigure(1, weight=1)

        tk.Label(frame_file, text=".py 파일 목록").grid(row=0, column=0, sticky="nw", pady=(2, 0))

        py_list_frame = tk.Frame(frame_file)
        py_list_frame.grid(row=0, column=1, padx=5, sticky="ew")
        py_list_frame.grid_columnconfigure(0, weight=1)

        self.py_listbox = tk.Listbox(py_list_frame, height=4, selectmode="extended")
        self.py_listbox.grid(row=0, column=0, sticky="ew")
        self.py_listbox.bind("<<ListboxSelect>>", self._on_py_listbox_select)

        scrollbar = tk.Scrollbar(py_list_frame, orient="vertical", command=self.py_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.py_listbox.configure(yscrollcommand=scrollbar.set)

        py_btn_frame = tk.Frame(frame_file)
        py_btn_frame.grid(row=0, column=2, sticky="n", padx=(5, 0))
        tk.Button(py_btn_frame, text="파일 추가", width=9, command=self.add_py_files).pack(pady=(0, 4))
        tk.Button(py_btn_frame, text="선택 제거", width=9, command=self.remove_selected_py).pack(pady=(0, 4))
        tk.Button(py_btn_frame, text="전체 제거", width=9, command=self.clear_py_files).pack()

        tk.Label(frame_file, text="출력 폴더").grid(row=1, column=0, sticky="w", pady=(8, 0))
        tk.Entry(frame_file, textvariable=self.output_dir).grid(row=1, column=1, padx=5, pady=(8, 0), sticky="ew")
        tk.Button(frame_file, text="찾기", command=self.select_output_dir).grid(row=1, column=2, pady=(8, 0))

        tk.Label(frame_file, text="EXE 파일명").grid(row=2, column=0, sticky="w", pady=(5, 0))
        exe_frame = tk.Frame(frame_file)
        exe_frame.grid(row=2, column=1, padx=5, pady=(5, 0), sticky="ew")
        exe_frame.grid_columnconfigure(0, weight=1)
        self.exe_name_entry = tk.Entry(exe_frame, textvariable=self.exe_name)
        self.exe_name_entry.grid(row=0, column=0, sticky="ew")
        self.exe_name_hint = tk.Label(exe_frame, text=".exe", fg="gray")
        self.exe_name_hint.grid(row=0, column=1, padx=(3, 0))
        tk.Label(
            frame_file, text="※ 파일이 2개 이상이면 각 파일명을 자동 사용",
            fg="gray", font=("", 8)
        ).grid(row=3, column=1, sticky="w", padx=5)

        # row1: 중간 영역
        frame_mid = tk.Frame(self, padx=10, pady=5)
        frame_mid.grid(row=1, column=0, sticky="nsew")
        frame_mid.grid_columnconfigure(1, weight=1)
        frame_mid.grid_columnconfigure(2, weight=1)
        frame_mid.grid_rowconfigure(0, weight=1)

        # ── 왼쪽: 빌드 옵션 ──────────────────────────────────────────
        frame_opt = tk.LabelFrame(frame_mid, text="빌드 옵션", padx=10, pady=10)
        frame_opt.grid(row=0, column=0, sticky="ns")
        frame_opt.grid_columnconfigure(0, weight=1)

        r = 0
        tk.Label(frame_opt, text="빌드 모드").grid(row=r, column=0, sticky="w"); r += 1
        tk.Radiobutton(frame_opt, text="단일 파일 (onefile)", variable=self.build_mode,
                       value="onefile", command=self._on_mode_change).grid(row=r, column=0, sticky="w"); r += 1
        tk.Radiobutton(frame_opt, text="폴더 모드 (onedir) ★권장",
                       variable=self.build_mode, value="onedir",
                       command=self._on_mode_change).grid(row=r, column=0, sticky="w"); r += 1
        tk.Label(
            frame_opt,
            text="  onedir: 실행속도↑ / 복수PC 공유 적합\n  onefile: 파일 1개 / 실행 시 임시폴더 압축해제→느림",
            fg="#888888", font=("", 8), justify="left"
        ).grid(row=r, column=0, sticky="w"); r += 1

        ttk.Separator(frame_opt, orient="horizontal").grid(row=r, column=0, sticky="ew", pady=(8, 4)); r += 1

        # 업데이트 가능 구조
        self.chk_updatable = tk.Checkbutton(
            frame_opt,
            text="업데이트 가능 구조 (onedir 전용)",
            variable=self.updatable_mode,
            command=self._on_updatable_toggle,
            fg="#1a5276", font=("", 9, "bold")
        )
        self.chk_updatable.grid(row=r, column=0, sticky="w"); r += 1
        tk.Label(
            frame_opt,
            text="  EXE 재인가 없이 .py 교체만으로 업데이트\n"
                 "  구조: AppName.exe + _internal/ + scripts/*.py",
            fg="#1a5276", font=("", 8), justify="left"
        ).grid(row=r, column=0, sticky="w"); r += 1

        ttk.Separator(frame_opt, orient="horizontal").grid(row=r, column=0, sticky="ew", pady=(8, 4)); r += 1

        # 바이트코드 최적화
        tk.Label(frame_opt, text="바이트코드 최적화 (--optimize)").grid(row=r, column=0, sticky="w"); r += 1
        opt_frame = tk.Frame(frame_opt)
        opt_frame.grid(row=r, column=0, sticky="w"); r += 1
        for text, val in [("없음(0)", "0"), ("기본(1)", "1"), ("적극적(2)", "2")]:
            tk.Radiobutton(opt_frame, text=text, variable=self.opt_level, value=val).pack(side="left")
        tk.Label(
            frame_opt, text="  1=assert/docstring 제거  2=추가 최적화",
            fg="#888888", font=("", 8), justify="left"
        ).grid(row=r, column=0, sticky="w"); r += 1

        # strip
        tk.Checkbutton(
            frame_opt, text="디버그 심볼 제거 (--strip)  ※Windows 미지원",
            variable=self.use_strip
        ).grid(row=r, column=0, sticky="w", pady=(4, 0)); r += 1

        ttk.Separator(frame_opt, orient="horizontal").grid(row=r, column=0, sticky="ew", pady=(8, 4)); r += 1

        tk.Checkbutton(
            frame_opt, text="콘솔 창 숨기기 (--noconsole)",
            variable=self.noconsole
        ).grid(row=r, column=0, sticky="w"); r += 1

        tk.Checkbutton(
            frame_opt, text="고급 용량 최적화 사용",
            variable=self.enable_advanced, command=self._on_advanced_toggle
        ).grid(row=r, column=0, sticky="w", pady=(5, 0)); r += 1

        self.chk_upx = tk.Checkbutton(frame_opt, text="UPX 압축 사용 (고급)",
                                       variable=self.use_upx, state="disabled")
        self.chk_upx.grid(row=r, column=0, sticky="w", pady=(5, 0)); r += 1

        # runtime tmpdir
        self.frame_rtmp = tk.LabelFrame(frame_opt, text="Runtime tmpdir (onefile 전용)", padx=10, pady=10)
        self.frame_rtmp.grid(row=r, column=0, sticky="ew", pady=(12, 0)); r += 1
        self.frame_rtmp.grid_columnconfigure(0, weight=1)

        tk.Checkbutton(
            self.frame_rtmp, text="runtime tmp 경로 사용",
            variable=self.enable_runtime_tmpdir
        ).grid(row=0, column=0, sticky="w")

        row_rtmp = tk.Frame(self.frame_rtmp)
        row_rtmp.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        row_rtmp.grid_columnconfigure(0, weight=1)
        tk.Entry(row_rtmp, textvariable=self.runtime_tmpdir).grid(row=0, column=0, sticky="ew")
        tk.Button(row_rtmp, text="폴더 선택", command=self.select_runtime_tmpdir).grid(row=0, column=1, padx=(8, 0))

        # hidden / collect
        frame_hidden = tk.LabelFrame(frame_opt, text="hidden-import / collect", padx=10, pady=10)
        frame_hidden.grid(row=r, column=0, sticky="ew", pady=(12, 0)); r += 1
        frame_hidden.grid_columnconfigure(0, weight=1)

        tk.Label(frame_hidden, text="Hidden imports (줄/쉼표)").grid(row=0, column=0, sticky="w")
        self.txt_hidden = tk.Text(frame_hidden, height=3)
        self.txt_hidden.grid(row=1, column=0, sticky="ew", pady=(2, 6))

        tk.Label(frame_hidden, text="Collect-all packages").grid(row=2, column=0, sticky="w")
        self.txt_collect_all = tk.Text(frame_hidden, height=2)
        self.txt_collect_all.grid(row=3, column=0, sticky="ew", pady=(2, 6))

        tk.Label(frame_hidden, text="Collect-submodules packages").grid(row=4, column=0, sticky="w")
        self.txt_collect_sub = tk.Text(frame_hidden, height=2)
        self.txt_collect_sub.grid(row=5, column=0, sticky="ew", pady=(2, 6))

        tk.Label(frame_hidden, text="Collect-data packages").grid(row=6, column=0, sticky="w")
        self.txt_collect_data = tk.Text(frame_hidden, height=2)
        self.txt_collect_data.grid(row=7, column=0, sticky="ew")

        # ── 중간: import 분석 ─────────────────────────────────────────
        frame_import = tk.LabelFrame(frame_mid, text="import 분석 (exclude 후보)", padx=10, pady=10)
        frame_import.grid(row=0, column=1, sticky="nsew", padx=(10, 5))
        frame_import.grid_rowconfigure(1, weight=1)
        frame_import.grid_columnconfigure(0, weight=1)

        tk.Label(frame_import, text="※ 목록에서 파일을 클릭하면 import가 분석됩니다.").grid(row=0, column=0, sticky="w")
        self.import_listbox = tk.Listbox(frame_import, selectmode="multiple")
        self.import_listbox.grid(row=1, column=0, sticky="nsew", pady=(5, 5))
        tk.Button(frame_import, text="import 재분석",
                  command=self.analyze_imports_for_selected_file).grid(row=2, column=0, sticky="e")

        # ── 오른쪽: 추천 후보 ─────────────────────────────────────────
        frame_reco = tk.LabelFrame(frame_mid, text="hidden-import 추천 후보 (자동 추출)", padx=10, pady=10)
        frame_reco.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        frame_reco.grid_rowconfigure(1, weight=1)
        frame_reco.grid_columnconfigure(0, weight=1)

        tk.Label(frame_reco, text="빌드/런타임 로그에서 자동 추출된 후보").grid(row=0, column=0, sticky="w")
        self.reco_listbox = tk.Listbox(frame_reco, selectmode="extended")
        self.reco_listbox.grid(row=1, column=0, sticky="nsew", pady=(5, 5))

        btnrow = tk.Frame(frame_reco)
        btnrow.grid(row=2, column=0, sticky="ew")
        btnrow.grid_columnconfigure(0, weight=1)
        btnrow.grid_columnconfigure(1, weight=1)
        btnrow.grid_columnconfigure(2, weight=1)

        tk.Button(btnrow, text="추천→Hidden 추가",
                  command=self.apply_reco_to_hidden).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        tk.Button(btnrow, text="추천 새로고침",
                  command=self.refresh_recommendations_from_files).grid(row=0, column=1, sticky="ew", padx=5)
        tk.Button(btnrow, text="빌드결과 실행 테스트",
                  command=self.run_built_exe_and_extract).grid(row=0, column=2, sticky="ew", padx=(5, 0))

        # row2: 로그
        frame_log = tk.LabelFrame(self, text="빌드/실행 로그", padx=10, pady=10)
        frame_log.grid(row=2, column=0, sticky="nsew", padx=10, pady=(5, 10))
        frame_log.grid_rowconfigure(0, weight=1)
        frame_log.grid_columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(frame_log, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # row3: 하단 버튼
        frame_bottom = tk.Frame(self, padx=10, pady=8)
        frame_bottom.grid(row=3, column=0, sticky="ew")
        frame_bottom.grid_columnconfigure(0, weight=1)

        tk.Label(frame_bottom, textvariable=self.status_text, anchor="w").grid(row=0, column=0, sticky="ew")
        self.btn_build = tk.Button(frame_bottom, text="빌드 시작", command=self.start_build_thread)
        self.btn_build.grid(row=0, column=1, padx=(10, 5))
        self.btn_quit = tk.Button(frame_bottom, text="종료", command=self.destroy)
        self.btn_quit.grid(row=0, column=2)

        # 초기 상태 반영
        self._on_mode_change()

    # ------------------------------------------------------------------
    # 스레드 안전 로그
    # ------------------------------------------------------------------
    def _drain_log(self):
        try:
            while True:
                text = self._log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", text + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def append_log(self, text: str):
        self._log_queue.put(text)

    # ------------------------------------------------------------------
    # 파일 목록 관리
    # ------------------------------------------------------------------
    def add_py_files(self):
        paths = filedialog.askopenfilenames(
            title="파이썬(.py) 파일 선택 (복수 선택 가능)",
            filetypes=[("Python Files", "*.py"), ("All Files", "*.*")]
        )
        added = 0
        for p in paths:
            if p not in self.py_files:
                self.py_files.append(p)
                self.py_listbox.insert(tk.END, p)
                added += 1

        self._update_exe_name_state()

        if added > 0:
            if len(self.py_files) == 1 and not self.exe_name.get().strip():
                self.exe_name.set(Path(self.py_files[0]).stem)
            self.py_listbox.selection_clear(0, tk.END)
            self.py_listbox.selection_set(tk.END)
            self.analyze_imports_for_selected_file()

    def remove_selected_py(self):
        selected = list(self.py_listbox.curselection())
        for idx in reversed(selected):
            self.py_files.pop(idx)
            self.py_listbox.delete(idx)
        self._update_exe_name_state()
        self.import_listbox.delete(0, tk.END)
        self.import_modules = []

    def clear_py_files(self):
        self.py_files.clear()
        self.py_listbox.delete(0, tk.END)
        self.import_listbox.delete(0, tk.END)
        self.import_modules = []
        self._update_exe_name_state()

    def _update_exe_name_state(self):
        count = len(self.py_files)
        if count <= 1:
            self.exe_name_entry.configure(state="normal")
            self.exe_name_hint.configure(text=".exe", fg="gray")
            if count == 0:
                self.exe_name.set("")
        else:
            self.exe_name_entry.configure(state="disabled")
            self.exe_name_hint.configure(text="(파일 2개 이상: 각 파일명 자동 사용)", fg="gray")

    def _on_py_listbox_select(self, _event=None):
        self.analyze_imports_for_selected_file()

    # ------------------------------------------------------------------
    # UI 이벤트
    # ------------------------------------------------------------------
    def _on_mode_change(self):
        """빌드 모드 변경 시 관련 UI 활성/비활성."""
        is_onefile = self.build_mode.get() == "onefile"
        # onefile일 때 업데이트 가능 구조 비활성
        if is_onefile:
            self.updatable_mode.set(False)
            self.chk_updatable.configure(state="disabled")
        else:
            self.chk_updatable.configure(state="normal")
        # runtime tmpdir 는 onefile 전용
        state = "normal" if is_onefile else "disabled"
        for child in self.frame_rtmp.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass

    def _on_updatable_toggle(self):
        """업데이트 가능 구조 체크 시 onedir 강제."""
        if self.updatable_mode.get():
            self.build_mode.set("onedir")
            self._on_mode_change()

    def _on_advanced_toggle(self):
        if self.enable_advanced.get():
            self.chk_upx.configure(state="normal")
        else:
            self.chk_upx.configure(state="disabled")
            self.use_upx.set(False)

    def select_output_dir(self):
        folder = filedialog.askdirectory(title="출력 폴더 선택")
        if folder:
            self.output_dir.set(folder)

    def select_runtime_tmpdir(self):
        folder = filedialog.askdirectory(title="runtime tmp 폴더 선택")
        if folder:
            self.runtime_tmpdir.set(folder)

    # ------------------------------------------------------------------
    # import 분석
    # ------------------------------------------------------------------
    def analyze_imports_for_selected_file(self):
        sel = self.py_listbox.curselection()
        py_path = self.py_files[sel[-1]] if sel else (self.py_files[0] if self.py_files else "")

        self.import_modules = []
        self.import_listbox.delete(0, tk.END)

        if not py_path or not os.path.isfile(py_path):
            return

        try:
            modules = sorted(collect_imports(py_path))
            self.import_modules = modules
            for m in modules:
                label = f"[*] {m}" if m in HEAVY_MODULE_HINTS else m
                self.import_listbox.insert(tk.END, label)

            self.append_log(f"=== import 분석 완료: {Path(py_path).name} ===")
            self.append_log("발견된 모듈: " + ", ".join(modules))
        except Exception as e:
            self.append_log(f"import 분석 중 예외 발생: {e}")

    # ------------------------------------------------------------------
    # 추천 후보
    # ------------------------------------------------------------------
    def _set_reco_list(self, candidates):
        self.reco_listbox.delete(0, tk.END)
        for c in candidates:
            self.reco_listbox.insert(tk.END, c)

    def apply_reco_to_hidden(self):
        selected = [self.reco_listbox.get(i) for i in self.reco_listbox.curselection()]
        if not selected:
            return
        current = parse_multiline_list(self.txt_hidden.get("1.0", "end"))
        merged = current[:]
        for x in selected:
            if x not in merged:
                merged.append(x)
        self.txt_hidden.delete("1.0", "end")
        self.txt_hidden.insert("1.0", "\n".join(merged))
        self.append_log("[INFO] 추천 후보를 Hidden imports에 추가했습니다: " + ", ".join(selected))

    def refresh_recommendations_from_files(self):
        out_dir = Path(self.output_dir.get().strip() or get_default_output_dir())
        candidates = set()

        log_text = self.log_text.get("1.0", "end")
        for c in extract_hidden_import_candidates(log_text):
            candidates.add(c)

        build_dir = out_dir / "build"
        if build_dir.exists():
            for warn_file in build_dir.rglob("warn-*.txt"):
                txt = safe_read_text(warn_file)
                for c in extract_hidden_import_candidates(txt):
                    candidates.add(c)

        final = sorted(candidates)
        self._set_reco_list(final)
        self.append_log(f"[INFO] 추천 후보 {len(final)}개 갱신 완료")

    def run_built_exe_and_extract(self):
        exe_path = self.last_exe_path
        if not exe_path or not Path(exe_path).exists():
            messagebox.showerror("오류", "실행 테스트할 빌드 결과(exe)를 찾지 못했습니다.\n먼저 빌드를 완료하세요.")
            return

        def _run():
            self.after(0, lambda: self.status_text.set("실행 테스트 중..."))
            self.append_log("======== 실행 테스트 시작 ========")
            self.append_log(f"EXE: {exe_path}")
            try:
                proc = subprocess.run([exe_path], capture_output=True, text=True, timeout=20)
                out = (proc.stdout or "") + "\n" + (proc.stderr or "")
                self.append_log(f"[RUN] returncode={proc.returncode}")
                if out.strip():
                    self.append_log("-------- stdout/stderr --------")
                    self.append_log(out.strip())
                    self.append_log("------------------------------")

                cands = extract_hidden_import_candidates(out)
                if cands:
                    self.append_log("[INFO] 실행 로그에서 hidden-import 후보 추출: " + ", ".join(cands))
                    existing = set(self.reco_listbox.get(0, tk.END))
                    existing.update(cands)
                    self.after(0, lambda s=sorted(existing): self._set_reco_list(s))
                else:
                    self.append_log("[INFO] 실행 로그에서 후보를 찾지 못했습니다.")

            except subprocess.TimeoutExpired:
                self.append_log("[WARN] 실행 테스트 타임아웃(20초).")
            except Exception as e:
                self.append_log(f"[ERROR] 실행 테스트 실패: {e}")
            finally:
                self.after(0, lambda: self.status_text.set("대기 중"))
                self.append_log("======== 실행 테스트 종료 ========")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # 빌드
    # ------------------------------------------------------------------
    def start_build_thread(self):
        if not self.py_files:
            messagebox.showerror("오류", ".py 파일을 하나 이상 추가해 주세요.")
            return

        # 업데이트 가능 구조는 단일 진입점에서만 지원
        if self.updatable_mode.get() and len(self.py_files) > 1:
            messagebox.showerror(
                "오류",
                "업데이트 가능 구조는 파일 1개(진입점)만 선택 시 사용 가능합니다.\n"
                "pages.py, widgets.py 등 보조 파일은 선택하지 않아도 자동으로 scripts/에 복사됩니다."
            )
            return

        # UI 상태를 메인 스레드에서 미리 수집 (스레드 안전)
        params = {
            "py_files": list(self.py_files),
            "out_dir": self.output_dir.get().strip() or get_default_output_dir(),
            "exe_name": self.exe_name.get().strip(),
            "build_mode": self.build_mode.get(),
            "noconsole": self.noconsole.get(),
            "enable_advanced": self.enable_advanced.get(),
            "use_upx": self.use_upx.get(),
            "enable_runtime_tmpdir": self.enable_runtime_tmpdir.get(),
            "runtime_tmpdir": self.runtime_tmpdir.get().strip(),
            "opt_level": self.opt_level.get(),
            "use_strip": self.use_strip.get(),
            "updatable_mode": self.updatable_mode.get(),
            "selected_labels": [
                self.import_listbox.get(i) for i in self.import_listbox.curselection()
            ],
            "hidden_imports": parse_multiline_list(self.txt_hidden.get("1.0", "end")),
            "collect_all": parse_multiline_list(self.txt_collect_all.get("1.0", "end")),
            "collect_sub": parse_multiline_list(self.txt_collect_sub.get("1.0", "end")),
            "collect_data": parse_multiline_list(self.txt_collect_data.get("1.0", "end")),
        }
        threading.Thread(target=self._build_all, args=(params,), daemon=True).start()

    def _build_all(self, p: dict):
        self.after(0, lambda: self.btn_build.config(state="disabled"))

        py_files = p["py_files"]
        total = len(py_files)
        success_count = 0
        fail_count = 0

        self.append_log(f"======== 일괄 빌드 시작: 총 {total}개 파일 ========")

        for idx, py_path in enumerate(py_files, start=1):
            if total == 1 and p["exe_name"]:
                exe_name = re.sub(r"\.exe$", "", p["exe_name"], flags=re.IGNORECASE)
            else:
                exe_name = Path(py_path).stem

            self.append_log("")
            self.append_log(f"────── [{idx}/{total}] {Path(py_path).name} → {exe_name}.exe ──────")
            self.after(0, lambda i=idx, t=total, n=exe_name: self.status_text.set(
                f"빌드 중... ({i}/{t}) {n}.exe"))

            if p["updatable_mode"]:
                ok = self._build_updatable(py_path, exe_name, p)
            else:
                ok = self._build_single(py_path, exe_name, p)

            if ok:
                success_count += 1
            else:
                fail_count += 1

        self.append_log("")
        self.append_log(
            f"======== 일괄 빌드 완료: 성공 {success_count} / 실패 {fail_count} / 전체 {total} ========")
        self.after(0, lambda s=success_count, f=fail_count: self.status_text.set(
            f"완료 — 성공 {s}개 / 실패 {f}개"))
        self.after(0, self.refresh_recommendations_from_files)
        self.after(0, lambda: self.btn_build.config(state="normal"))

        if fail_count == 0:
            messagebox.showinfo("완료", f"전체 {total}개 파일 빌드 성공!")
        else:
            messagebox.showwarning("완료(일부 실패)",
                                   f"성공: {success_count}개\n실패: {fail_count}개\n\n로그를 확인해 주세요.")

    # ------------------------------------------------------------------
    # 단일 파일 빌드
    # ------------------------------------------------------------------
    def _build_single(self, py_path: str, exe_name: str, p: dict) -> bool:
        out_dir = p["out_dir"]

        if not os.path.isfile(py_path):
            self.append_log(f"[ERROR] 파일을 찾을 수 없습니다: {py_path}")
            return False

        self.append_log(f"  입력: {py_path}")
        self.append_log(f"  출력: {out_dir}\\{exe_name}.exe")
        self.append_log(f"  모드: {p['build_mode']}")

        cmd = self._build_cmd_base(p, exe_name)

        # 스크립트 디렉토리를 --paths 에 추가 (형제 모듈 자동 탐색)
        script_dir = str(Path(py_path).parent)
        cmd.extend(["--paths", script_dir])

        self._append_common_flags(cmd, p)
        cmd.append(py_path)

        return self._run_pyinstaller(cmd, exe_name, out_dir, p["build_mode"])

    # ------------------------------------------------------------------
    # 업데이트 가능 구조 빌드
    # ------------------------------------------------------------------
    def _build_updatable(self, py_path: str, exe_name: str, p: dict) -> bool:
        """
        구조:
          <out_dir>/<exe_name>/
            ├── <exe_name>.exe   (인가 대상, 절대 변경 안 됨)
            ├── _internal/       (Python 런타임 + 라이브러리)
            └── scripts/         (업무 로직 .py — 여기만 교체)
        """
        out_dir = p["out_dir"]
        script_dir = Path(py_path).parent
        main_name = Path(py_path).name

        self.append_log(f"  [업데이트 가능 구조] 진입점: {main_name}")
        self.append_log(f"  스크립트 디렉토리: {script_dir}")

        # 같은 디렉토리의 .py 파일들 (로컬 모듈)
        local_py_files = list(script_dir.glob("*.py"))
        local_module_names = {f.stem for f in local_py_files}
        self.append_log(f"  scripts/ 에 복사할 .py: {[f.name for f in local_py_files]}")

        # 진입점에서 import된 모듈 중 시스템 패키지만 추출
        all_imports = collect_imports(py_path)
        # 로컬 .py 파일과 일치하는 모듈 이름은 scripts/에서 로드되므로 제외
        system_imports = sorted(
            m for m in all_imports
            if m not in local_module_names and m not in {"__future__", "sys", "os"}
        )
        self.append_log(f"  런처에 명시할 시스템 패키지: {system_imports}")

        # 런처 생성 (임시 파일)
        dep_lines = "\n".join(
            f"try:\n    import {m}  # noqa\nexcept ImportError:\n    pass"
            for m in system_imports
        )
        launcher_src = _LAUNCHER_TEMPLATE.format(
            dep_imports=dep_lines,
            main_script=main_name,
        )

        tmp_dir = Path(tempfile.mkdtemp(prefix="pyexe_launcher_"))
        launcher_path = tmp_dir / f"_launcher_{exe_name}.py"
        try:
            launcher_path.write_text(launcher_src, encoding="utf-8")
            self.append_log(f"  런처 생성: {launcher_path}")

            # 강제 onedir
            build_params = dict(p)
            build_params["build_mode"] = "onedir"
            build_params["enable_runtime_tmpdir"] = False

            cmd = self._build_cmd_base(build_params, exe_name)
            # 런처가 script_dir의 모듈을 참조하므로 --paths 추가
            cmd.extend(["--paths", str(script_dir)])
            self._append_common_flags(cmd, build_params)
            cmd.append(str(launcher_path))

            ok = self._run_pyinstaller(cmd, exe_name, out_dir, "onedir")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not ok:
            return False

        # 빌드 성공 후 scripts/ 폴더에 .py 복사
        dist_app_dir = Path(out_dir) / exe_name
        scripts_dir = dist_app_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)

        copied = []
        for src_file in local_py_files:
            dst = scripts_dir / src_file.name
            shutil.copy2(src_file, dst)
            copied.append(src_file.name)

        self.append_log(f"  [OK] scripts/ 복사 완료: {copied}")

        # ── launch.bat 생성 (네트워크 DLL 로드 차단 우회) ──────────────
        bat_content = _BAT_TEMPLATE.format(app_name=exe_name)
        bat_path = dist_app_dir / "launch.bat"
        try:
            bat_path.write_text(bat_content, encoding="utf-8")
            self.append_log(f"  [OK] launch.bat 생성: {bat_path}")
        except Exception as e:
            self.append_log(f"  [WARN] launch.bat 생성 실패: {e}")

        # ── version.txt 생성 (업데이트 감지용) ─────────────────────────
        import datetime
        ver_content = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        ver_path = dist_app_dir / "version.txt"
        try:
            ver_path.write_text(ver_content, encoding="utf-8")
            self.append_log(f"  [OK] version.txt 생성: {ver_content}")
        except Exception as e:
            self.append_log(f"  [WARN] version.txt 생성 실패: {e}")

        self.append_log("")
        self.append_log(f"  ┌── 배포 구조 ───────────────────────────────────────")
        self.append_log(f"  │  {dist_app_dir}/")
        self.append_log(f"  │  ├── launch.bat     ← ★ 사용자는 이것만 실행")
        self.append_log(f"  │  ├── {exe_name}.exe  ← 인가 대상 (절대 안 바뀜)")
        self.append_log(f"  │  ├── _internal/      ← 런타임 (변경 불필요)")
        self.append_log(f"  │  ├── scripts/        ← 업무 로직")
        for f in copied:
            self.append_log(f"  │  │    └── {f}")
        self.append_log(f"  │  └── version.txt    ← 업데이트 감지용")
        self.append_log(f"  └───────────────────────────────────────────────────")
        self.append_log("")
        self.append_log("  ★ 사용자 실행 방법: launch.bat 실행")
        self.append_log("     → 첫 실행: %LOCALAPPDATA%에 전체 복사 후 실행")
        self.append_log("     → 이후: version.txt 비교 후 변경 시만 scripts/ 동기화")
        self.append_log("  ★ 업데이트 배포: scripts/*.py 교체 + version.txt 갱신")
        self.append_log("  ★ EXE 재인가 불필요. 로컬 실행이므로 DLL 차단 없음.")
        return True

    # ------------------------------------------------------------------
    # PyInstaller 명령 빌더 헬퍼
    # ------------------------------------------------------------------
    def _build_cmd_base(self, p: dict, exe_name: str) -> list[str]:
        cmd_base = DEFAULT_PYINSTALLER_CMD.split()

        if p["build_mode"] == "onedir":
            cmd = cmd_base + ["--onedir", "--clean"]
        else:
            cmd = cmd_base + ["--onefile", "--clean"]

        cmd.extend(["--name", exe_name])

        opt = int(p.get("opt_level", "1"))
        if opt > 0:
            cmd.extend(["--optimize", str(opt)])
            self.append_log(f"  [OPT] --optimize {opt}")

        if p.get("use_strip"):
            cmd.append("--strip")
            self.append_log("  [OPT] --strip")

        if p["noconsole"]:
            cmd.append("--noconsole")

        if p["build_mode"] == "onefile" and p["enable_runtime_tmpdir"]:
            rtmp = p["runtime_tmpdir"]
            if rtmp:
                cmd.extend(["--runtime-tmpdir", rtmp])
                self.append_log(f"  [INFO] runtime tmpdir: {rtmp}")
            else:
                self.append_log("  [WARN] runtime tmpdir 경로가 비어있습니다.")

        out_dir = p["out_dir"]
        cmd.extend(["--distpath", out_dir])
        cmd.extend(["--workpath", os.path.join(out_dir, "build")])
        cmd.extend(["--specpath", out_dir])

        for m in DEFAULT_EXCLUDES:
            cmd.extend(["--exclude-module", m])

        if p["enable_advanced"]:
            for label in p["selected_labels"]:
                mod_name = label[4:] if label.startswith("[*] ") else label
                cmd.extend(["--exclude-module", mod_name])
            if p["use_upx"]:
                upx_dir = UPX_PATH.strip()
                if upx_dir:
                    cmd.extend(["--upx-dir", upx_dir])
                    self.append_log(f"  [INFO] UPX 경로: {upx_dir}")
                else:
                    self.append_log("  [INFO] UPX PATH 자동 탐색")

        return cmd

    def _append_common_flags(self, cmd: list[str], p: dict):
        for h in p["hidden_imports"]:
            cmd.extend(["--hidden-import", h])
        for pkg in p["collect_all"]:
            cmd.extend(["--collect-all", pkg])
        for pkg in p["collect_sub"]:
            cmd.extend(["--collect-submodules", pkg])
        for pkg in p["collect_data"]:
            cmd.extend(["--collect-data", pkg])

    def _run_pyinstaller(self, cmd: list[str], exe_name: str, out_dir: str, build_mode: str) -> bool:
        self.append_log("  실행 명령: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

        # 시스템 기본 인코딩 사용 → 한글 경로 로그 정상 출력
        sys_enc = locale.getpreferredencoding(False) or "utf-8"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding=sys_enc,
                errors="replace"
            )

            for line in proc.stdout:
                self.append_log(line.rstrip("\n"))

            proc.wait()

            if proc.returncode == 0:
                self.append_log(f"  [OK] {exe_name}.exe 빌드 성공")
                dist = Path(out_dir)
                if build_mode == "onedir":
                    exe1 = dist / exe_name / f"{exe_name}.exe"
                    exe2 = dist / f"{exe_name}.exe"
                    self.last_exe_path = str(exe1 if exe1.exists() else exe2 if exe2.exists() else "")
                else:
                    exe = dist / f"{exe_name}.exe"
                    self.last_exe_path = str(exe) if exe.exists() else ""
                self.last_dist_dir = out_dir
                self.last_app_name = exe_name
                return True
            else:
                self.append_log(f"  [FAIL] 빌드 실패 (returncode={proc.returncode})")
                return False

        except FileNotFoundError:
            self.append_log("  [ERROR] PyInstaller를 찾을 수 없습니다. pip install pyinstaller 를 실행하세요.")
            return False
        except Exception as e:
            self.append_log(f"  [ERROR] 예외 발생: {e}")
            return False


if __name__ == "__main__":
    app = ExeBuilderApp()
    app.mainloop()
