import os
import sys
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import ast
from pathlib import Path

# -----------------------------
# 설정
# -----------------------------
DEFAULT_PYINSTALLER_CMD = "pyinstaller"   # 필요 시 "python -m PyInstaller"
UPX_PATH = ""                              # PATH에 있으면 빈 문자열 가능

DEFAULT_EXCLUDES = ["tkinter.test", "test", "unittest"]

HEAVY_MODULE_HINTS = {
    "numpy", "pandas", "matplotlib", "scipy", "sklearn",
    "tensorflow", "torch", "tkinter", "PyQt5", "PySide6",
    "opencv", "cv2",
}


def get_default_output_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_multiline_list(text: str):
    """
    여러 줄/쉼표/세미콜론 입력을 리스트로 변환
    - 공백/빈 줄 제거
    - 중복 제거(순서 유지)
    """
    if not text:
        return []
    raw = text.replace(",", "\n").replace(";", "\n").splitlines()
    items = []
    for x in raw:
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


def extract_hidden_import_candidates(text: str):
    """
    로그 텍스트에서 hidden-import 후보 추출
    """
    cands = set()

    # ModuleNotFoundError: No module named 'xxx'
    for m in re.findall(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", text):
        cands.add(m.strip())

    # ImportError: No module named xxx
    for m in re.findall(r"ImportError:\s+No module named\s+([A-Za-z0-9_\.]+)", text):
        cands.add(m.strip())

    # PyInstaller warnings: missing module named 'xxx'
    for m in re.findall(r"missing module named ['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE):
        cands.add(m.strip())

    # Hidden import 'xxx' ...
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

        self.geometry("950x650")
        self.minsize(950, 650)
        self.resizable(True, True)

        # state
        self.py_path = tk.StringVar()
        self.output_dir = tk.StringVar(value=get_default_output_dir())
        self.build_mode = tk.StringVar(value="onefile")  # onefile / onedir

        self.enable_advanced = tk.BooleanVar(value=False)
        self.use_upx = tk.BooleanVar(value=False)

        # runtime tmpdir (onefile 전용)
        self.enable_runtime_tmpdir = tk.BooleanVar(value=False)
        self.runtime_tmpdir = tk.StringVar(value="")

        self.status_text = tk.StringVar(value="대기 중")

        # import analysis
        self.import_modules = []
        self.import_listbox = None

        # recommendations
        self.reco_listbox = None

        # tracking last build outputs
        self.last_dist_dir = None
        self.last_app_name = None
        self.last_exe_path = None

        self._build_ui_grid()

    # -----------------------------
    # UI (grid 기반)
    # -----------------------------
    def _build_ui_grid(self):
        self.grid_rowconfigure(1, weight=1)  # middle expand
        self.grid_rowconfigure(2, weight=1)  # log expand
        self.grid_columnconfigure(0, weight=1)

        # row0: file
        frame_file = tk.Frame(self, padx=10, pady=10)
        frame_file.grid(row=0, column=0, sticky="ew")
        frame_file.grid_columnconfigure(1, weight=1)

        tk.Label(frame_file, text=".py 파일 경로").grid(row=0, column=0, sticky="w")
        tk.Entry(frame_file, textvariable=self.py_path).grid(row=0, column=1, padx=5, sticky="ew")
        tk.Button(frame_file, text="찾기", command=self.select_py).grid(row=0, column=2)

        tk.Label(frame_file, text="출력 폴더").grid(row=1, column=0, sticky="w", pady=(5, 0))
        tk.Entry(frame_file, textvariable=self.output_dir).grid(row=1, column=1, padx=5, pady=(5, 0), sticky="ew")
        tk.Button(frame_file, text="찾기", command=self.select_output_dir).grid(row=1, column=2, pady=(5, 0))

        # row1: mid (options + import + reco)
        frame_mid = tk.Frame(self, padx=10, pady=5)
        frame_mid.grid(row=1, column=0, sticky="nsew")
        frame_mid.grid_columnconfigure(1, weight=1)
        frame_mid.grid_columnconfigure(2, weight=1)
        frame_mid.grid_rowconfigure(0, weight=1)

        # left: options
        frame_opt = tk.LabelFrame(frame_mid, text="빌드 옵션", padx=10, pady=10)
        frame_opt.grid(row=0, column=0, sticky="ns")
        frame_opt.grid_columnconfigure(0, weight=1)

        tk.Label(frame_opt, text="빌드 모드").grid(row=0, column=0, sticky="w")
        tk.Radiobutton(frame_opt, text="용량 우선 (onefile)", variable=self.build_mode, value="onefile").grid(row=1, column=0, sticky="w")
        tk.Radiobutton(frame_opt, text="안정 모드 (onedir)", variable=self.build_mode, value="onedir").grid(row=2, column=0, sticky="w")

        tk.Checkbutton(
            frame_opt, text="고급 용량 최적화 사용",
            variable=self.enable_advanced, command=self._on_advanced_toggle
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

        self.chk_upx = tk.Checkbutton(frame_opt, text="UPX 압축 사용 (고급)", variable=self.use_upx, state="disabled")
        self.chk_upx.grid(row=4, column=0, sticky="w", pady=(5, 0))

        # runtime tmpdir (onefile only)
        frame_rtmp = tk.LabelFrame(frame_opt, text="Runtime tmpdir (onefile 전용)", padx=10, pady=10)
        frame_rtmp.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        frame_rtmp.grid_columnconfigure(0, weight=1)

        tk.Checkbutton(
            frame_rtmp,
            text="runtime tmp 경로 사용",
            variable=self.enable_runtime_tmpdir
        ).grid(row=0, column=0, sticky="w")

        row_rtmp = tk.Frame(frame_rtmp)
        row_rtmp.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        row_rtmp.grid_columnconfigure(0, weight=1)

        tk.Entry(row_rtmp, textvariable=self.runtime_tmpdir).grid(row=0, column=0, sticky="ew")
        tk.Button(row_rtmp, text="폴더 선택", command=self.select_runtime_tmpdir).grid(row=0, column=1, padx=(8, 0))

        # hidden/collect inputs
        frame_hidden = tk.LabelFrame(frame_opt, text="hidden-import / collect", padx=10, pady=10)
        frame_hidden.grid(row=6, column=0, sticky="ew", pady=(12, 0))
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

        # middle: import analysis list
        frame_import = tk.LabelFrame(frame_mid, text="import 분석 (exclude 후보)", padx=10, pady=10)
        frame_import.grid(row=0, column=1, sticky="nsew", padx=(10, 5))
        frame_import.grid_rowconfigure(1, weight=1)
        frame_import.grid_columnconfigure(0, weight=1)

        tk.Label(frame_import, text="※ 선택한 모듈을 exclude 하면 실행 오류가 날 수 있습니다.").grid(row=0, column=0, sticky="w")

        self.import_listbox = tk.Listbox(frame_import, selectmode="multiple")
        self.import_listbox.grid(row=1, column=0, sticky="nsew", pady=(5, 5))

        tk.Button(frame_import, text="import 재분석", command=self.analyze_imports_for_selected_file).grid(row=2, column=0, sticky="e")

        # right: recommendations
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

        tk.Button(btnrow, text="추천→Hidden 추가", command=self.apply_reco_to_hidden).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        tk.Button(btnrow, text="추천 새로고침", command=self.refresh_recommendations_from_files).grid(row=0, column=1, sticky="ew", padx=5)
        tk.Button(btnrow, text="빌드결과 실행 테스트", command=self.run_built_exe_and_extract).grid(row=0, column=2, sticky="ew", padx=(5, 0))

        # row2: log
        frame_log = tk.LabelFrame(self, text="빌드/실행 로그", padx=10, pady=10)
        frame_log.grid(row=2, column=0, sticky="nsew", padx=10, pady=(5, 10))
        frame_log.grid_rowconfigure(0, weight=1)
        frame_log.grid_columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(frame_log, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # row3: bottom (always visible)
        frame_bottom = tk.Frame(self, padx=10, pady=8)
        frame_bottom.grid(row=3, column=0, sticky="ew")
        frame_bottom.grid_columnconfigure(0, weight=1)
        frame_bottom.grid_columnconfigure(1, weight=0)
        frame_bottom.grid_columnconfigure(2, weight=0)

        tk.Label(frame_bottom, textvariable=self.status_text, anchor="w").grid(row=0, column=0, sticky="ew")
        self.btn_build = tk.Button(frame_bottom, text="빌드 시작", command=self.start_build_thread)
        self.btn_build.grid(row=0, column=1, padx=(10, 5))
        self.btn_quit = tk.Button(frame_bottom, text="종료", command=self.destroy)
        self.btn_quit.grid(row=0, column=2)

    # -----------------------------
    # UI actions
    # -----------------------------
    def append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_advanced_toggle(self):
        if self.enable_advanced.get():
            self.chk_upx.configure(state="normal")
        else:
            self.chk_upx.configure(state="disabled")
            self.use_upx.set(False)

    def select_py(self):
        file_path = filedialog.askopenfilename(
            title="파이썬(.py) 파일 선택",
            filetypes=[("Python Files", "*.py"), ("All Files", "*.*")]
        )
        if file_path:
            self.py_path.set(file_path)
            self.analyze_imports_for_selected_file()

    def select_output_dir(self):
        folder = filedialog.askdirectory(title="출력 폴더 선택")
        if folder:
            self.output_dir.set(folder)

    def select_runtime_tmpdir(self):
        folder = filedialog.askdirectory(title="runtime tmp 폴더 선택")
        if folder:
            self.runtime_tmpdir.set(folder)

    # -----------------------------
    # import analysis
    # -----------------------------
    def analyze_imports_for_selected_file(self):
        py_path = self.py_path.get().strip()
        self.import_modules = []
        self.import_listbox.delete(0, tk.END)

        if not py_path or not os.path.isfile(py_path):
            return

        try:
            with open(py_path, "r", encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src, filename=py_path)

            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        modules.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        modules.add(node.module.split(".")[0])

            self.import_modules = sorted(modules)
            for m in self.import_modules:
                label = f"[*] {m}" if m in HEAVY_MODULE_HINTS else m
                self.import_listbox.insert(tk.END, label)

            self.append_log("=== import 분석 완료 ===")
            self.append_log("발견된 모듈: " + ", ".join(self.import_modules))
        except Exception as e:
            self.append_log(f"import 분석 중 예외 발생: {e}")

    # -----------------------------
    # recommendations
    # -----------------------------
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
        """
        현재 로그 + build 하위 warn-*.txt에서 후보 추출
        """
        out_dir = Path((self.output_dir.get().strip() or get_default_output_dir()))
        candidates = set()

        # 1) 현재 로그창
        log_text = self.log_text.get("1.0", "end")
        for c in extract_hidden_import_candidates(log_text):
            candidates.add(c)

        # 2) build 하위 warn-*.txt
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
        """
        빌드 산출물을 실행해보고(가능한 경우) stdout/stderr에서 후보 추출.
        네트워크 실행 제한 환경에서는 실패할 수 있음.
        """
        exe_path = self.last_exe_path
        if not exe_path or not Path(exe_path).exists():
            messagebox.showerror("오류", "실행 테스트할 빌드 결과(exe)를 찾지 못했습니다.\n먼저 빌드를 완료하세요.")
            return

        def _run():
            self.status_text.set("실행 테스트 중...")
            self.append_log("======== 실행 테스트 시작 ========")
            self.append_log(f"EXE: {exe_path}")

            try:
                proc = subprocess.run(
                    [exe_path],
                    capture_output=True,
                    text=True,
                    timeout=20
                )
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
                    for c in cands:
                        existing.add(c)
                    self._set_reco_list(sorted(existing))
                else:
                    self.append_log("[INFO] 실행 로그에서 후보를 찾지 못했습니다.")

            except subprocess.TimeoutExpired:
                self.append_log("[WARN] 실행 테스트 타임아웃(20초). 프로그램이 계속 실행 중일 수 있습니다.")
            except Exception as e:
                self.append_log(f"[ERROR] 실행 테스트 실패: {e}")
            finally:
                self.status_text.set("대기 중")
                self.append_log("======== 실행 테스트 종료 ========")

        threading.Thread(target=_run, daemon=True).start()

    # -----------------------------
    # build
    # -----------------------------
    def start_build_thread(self):
        threading.Thread(target=self.build_exe, daemon=True).start()

    def build_exe(self):
        py_path = self.py_path.get().strip()
        out_dir = (self.output_dir.get().strip() or get_default_output_dir())

        if not py_path or not os.path.isfile(py_path):
            messagebox.showerror("오류", ".py 파일을 올바르게 선택해 주세요.")
            return

        self.btn_build.config(state="disabled")
        self.status_text.set("빌드 중...")

        script_name = Path(py_path).stem
        self.last_dist_dir = out_dir
        self.last_app_name = script_name
        self.last_exe_path = None

        self.append_log("======== 빌드 시작 ========")
        self.append_log(f"입력 파일: {py_path}")
        self.append_log(f"출력 폴더: {out_dir}")
        self.append_log(f"빌드 모드: {self.build_mode.get()}")

        # base command
        if self.build_mode.get() == "onedir":
            cmd = [DEFAULT_PYINSTALLER_CMD, "--onedir", "--clean"]
        else:
            cmd = [DEFAULT_PYINSTALLER_CMD, "--onefile", "--clean"]

        # always no console for target exe
        cmd.append("--noconsole")

        # runtime tmpdir (onefile only)
        if self.build_mode.get() == "onefile" and self.enable_runtime_tmpdir.get():
            rtmp = (self.runtime_tmpdir.get() or "").strip()
            if rtmp:
                cmd.extend(["--runtime-tmpdir", rtmp])
                self.append_log(f"[INFO] runtime tmpdir 적용: {rtmp}")
            else:
                self.append_log("[WARN] runtime tmpdir 사용이 체크되어 있으나 경로가 비어있습니다.")

        # paths
        cmd.extend(["--distpath", out_dir])
        cmd.extend(["--workpath", os.path.join(out_dir, "build")])
        cmd.extend(["--specpath", out_dir])

        # baseline excludes
        for m in DEFAULT_EXCLUDES:
            cmd.extend(["--exclude-module", m])
            self.append_log(f"  - exclude-module(기본): {m}")

        # advanced excludes & UPX
        if self.enable_advanced.get():
            self.append_log("[INFO] 고급 용량 최적화 모드 활성화")

            selected_indices = self.import_listbox.curselection()
            selected_modules = []
            for idx in selected_indices:
                label = self.import_listbox.get(idx)
                mod_name = label[4:] if label.startswith("[*] ") else label
                selected_modules.append(mod_name)

            if selected_modules:
                self.append_log("선택된 제외 모듈: " + ", ".join(selected_modules))
                for m in selected_modules:
                    cmd.extend(["--exclude-module", m])
                    self.append_log(f"  - exclude-module(import 선택): {m}")
            else:
                self.append_log("선택된 import 제외 모듈 없음")

            if self.use_upx.get():
                upx_dir = UPX_PATH if UPX_PATH else ""
                cmd.extend(["--upx-dir", upx_dir])
                self.append_log(f"  - UPX 압축 사용 (경로: {upx_dir if upx_dir else 'PATH에서 검색'})")

        # hidden / collect
        hidden_imports = parse_multiline_list(self.txt_hidden.get("1.0", "end"))
        collect_all = parse_multiline_list(self.txt_collect_all.get("1.0", "end"))
        collect_sub = parse_multiline_list(self.txt_collect_sub.get("1.0", "end"))
        collect_data = parse_multiline_list(self.txt_collect_data.get("1.0", "end"))

        if hidden_imports:
            self.append_log("Hidden-import 적용: " + ", ".join(hidden_imports))
            for h in hidden_imports:
                cmd.extend(["--hidden-import", h])

        if collect_all:
            self.append_log("Collect-all 적용: " + ", ".join(collect_all))
            for p in collect_all:
                cmd.extend(["--collect-all", p])

        if collect_sub:
            self.append_log("Collect-submodules 적용: " + ", ".join(collect_sub))
            for p in collect_sub:
                cmd.extend(["--collect-submodules", p])

        if collect_data:
            self.append_log("Collect-data 적용: " + ", ".join(collect_data))
            for p in collect_data:
                cmd.extend(["--collect-data", p])

        cmd.append(py_path)

        self.append_log("실행 명령:")
        self.append_log(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        self.append_log("")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace"
            )

            for line in proc.stdout:
                self.append_log(line.rstrip("\n"))

            proc.wait()

            if proc.returncode == 0:
                self.status_text.set("빌드 완료")
                self.append_log("======== 빌드 성공 ========")

                dist = Path(out_dir)

                # estimate exe path for run-test
                if self.build_mode.get() == "onedir":
                    exe1 = dist / script_name / f"{script_name}.exe"
                    exe2 = dist / f"{script_name}.exe"
                    self.last_exe_path = str(exe1 if exe1.exists() else exe2 if exe2.exists() else "")
                else:
                    exe = dist / f"{script_name}.exe"
                    self.last_exe_path = str(exe) if exe.exists() else ""

                # refresh recommendations from build outputs
                self.refresh_recommendations_from_files()

                messagebox.showinfo("완료", "EXE 빌드가 완료되었습니다.\n필요 시 추천 후보를 Hidden imports에 추가 후 재빌드하세요.")
            else:
                self.status_text.set("빌드 실패")
                self.append_log("======== 빌드 실패 ========")

                self.refresh_recommendations_from_files()
                messagebox.showerror("오류", f"빌드 실패 (return code: {proc.returncode})")

        except FileNotFoundError:
            self.status_text.set("PyInstaller 실행 실패")
            self.append_log("PyInstaller를 찾을 수 없습니다. 설치 및 PATH 설정을 확인하세요.")
            messagebox.showerror("오류", "PyInstaller를 찾을 수 없습니다.\n예: pip install pyinstaller")
        except Exception as e:
            self.status_text.set("예외 발생")
            self.append_log(f"예외 발생: {e}")
            messagebox.showerror("오류", f"예외 발생: {e}")
        finally:
            self.btn_build.config(state="normal")


if __name__ == "__main__":
    app = ExeBuilderApp()
    app.mainloop()
